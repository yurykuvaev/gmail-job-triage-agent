"""Telegram Bot API sender.

Markdown messages, split to stay under Telegram's 4096-char per-message limit.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TELEGRAM_MAX = 4096
SAFE_CHUNK = 3800  # leave headroom for the (n/m) suffix.


def _split(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """Split on line boundaries; only break a line if a single line exceeds limit."""

    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
        # one absurdly long line: hard-split it.
        while current_len > limit:
            blob = "".join(current)
            chunks.append(blob[:limit])
            remainder = blob[limit:]
            current = [remainder]
            current_len = len(remainder)
    if current:
        chunks.append("".join(current))
    return chunks


def send_message(bot_token: str, chat_id: str, text: str) -> int:
    """Send a (possibly chunked) Markdown message. Returns count of API calls made."""

    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    chunks = _split(text)
    total = len(chunks)
    sent = 0
    with httpx.Client(timeout=30.0) as client:
        for idx, chunk in enumerate(chunks, start=1):
            body = chunk if total == 1 else f"{chunk}\n\n_({idx}/{total})_"
            r = client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": body,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            r.raise_for_status()
            sent += 1
    logger.info("telegram_sent", extra={"chunks": sent})
    return sent
