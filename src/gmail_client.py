"""Gmail fetch layer.

Uses a stored OAuth refresh token to mint short-lived access tokens.
Returns plain dicts so downstream code stays decoupled from the Google SDK.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_BODY_CHARS = 4000


@dataclass
class FetchedEmail:
    """Normalized representation of one Gmail message."""

    id: str
    thread_id: str
    subject: str
    sender: str
    received_at: datetime
    body: str

    def to_prompt_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "from": self.sender,
            "received_at": self.received_at.isoformat(),
            "body": self.body[:MAX_BODY_CHARS],
        }


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    return parser.text()


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree, preferring text/plain over text/html."""

    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            decoded = _decode_part(data)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return re.sub(r"\s+", " ", "\n".join(plain)).strip()
    if html:
        return _html_to_text("\n".join(html))
    return ""


def build_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> Credentials:
    """Hydrate Google credentials from a stored refresh token."""

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GMAIL_SCOPES,
    )
    creds.refresh(Request())
    return creds


def fetch_recent_emails(
    creds: Credentials, lookback_hours: int, max_results: int = 200
) -> list[FetchedEmail]:
    """Return inbox messages received in the last `lookback_hours`."""

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    after = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    # Gmail search uses epoch seconds for `after:`.
    query = f"in:inbox after:{int(after.timestamp())}"

    out: list[FetchedEmail] = []
    page_token: str | None = None
    fetched = 0
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=100)
            .execute()
        )
        for meta in resp.get("messages", []):
            if fetched >= max_results:
                break
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=meta["id"], format="full")
                .execute()
            )
            out.append(_to_fetched(msg))
            fetched += 1
        page_token = resp.get("nextPageToken")
        if not page_token or fetched >= max_results:
            break
    logger.info("gmail_fetch_done", extra={"count": len(out), "query": query})
    return out


def _to_fetched(msg: dict) -> FetchedEmail:
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("subject", "(no subject)")
    sender_raw = headers.get("from", "")
    _, sender_email = parseaddr(sender_raw)
    received_at = (
        parsedate_to_datetime(headers["date"])
        if "date" in headers
        else datetime.now(tz=timezone.utc)
    )
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    body = _extract_body(msg["payload"])
    return FetchedEmail(
        id=msg["id"],
        thread_id=msg["threadId"],
        subject=subject,
        sender=sender_raw or sender_email,
        received_at=received_at,
        body=body,
    )
