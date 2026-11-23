"""One-time local helper to generate a Gmail API OAuth2 refresh token.

Usage:
    uv run --extra setup python scripts/get_gmail_token.py path/to/client_secret.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: uv run --extra setup python scripts/get_gmail_token.py <client_secret.json>")
        return 1

    client_secret_path = Path(sys.argv[1])
    if not client_secret_path.is_file():
        print(f"File not found: {client_secret_path}")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    raw_cfg = json.loads(client_secret_path.read_text(encoding="utf-8"))
    client_info = raw_cfg.get("installed") or raw_cfg.get("web") or {}

    print("\nCopy these values into GitHub Repository Secrets (do not commit them):\n")
    print(f"GMAIL_CLIENT_ID={client_info.get('client_id', creds.client_id)}")
    print(f"GMAIL_CLIENT_SECRET={client_info.get('client_secret', creds.client_secret)}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
