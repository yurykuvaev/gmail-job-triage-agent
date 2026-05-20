"""One-time local OAuth flow to mint a Gmail refresh token.

Usage:
    python scripts/gmail_oauth_local.py path/to/oauth_client.json

Where oauth_client.json is the "Desktop app" OAuth client JSON downloaded
from Google Cloud Console (APIs & Services -> Credentials).

The refresh token printed at the end goes into AWS Secrets Manager.
Never commit it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/gmail_oauth_local.py <oauth_client.json>", file=sys.stderr)
        return 2

    client_path = Path(sys.argv[1])
    if not client_path.exists():
        print(f"file not found: {client_path}", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    # access_type='offline' + prompt='consent' is what produces a refresh token.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="A browser window will open — sign in with your Gmail account.",
    )

    with client_path.open() as f:
        client_json = json.load(f)
    cfg = client_json.get("installed") or client_json.get("web") or {}
    client_id = cfg.get("client_id", "")
    client_secret = cfg.get("client_secret", "")

    print("\n--- Copy these into AWS Secrets Manager ---")
    print(json.dumps({
        "gmail_client_id":     client_id,
        "gmail_client_secret": client_secret,
        "gmail_refresh_token": creds.refresh_token,
    }, indent=2))
    print("-------------------------------------------\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
