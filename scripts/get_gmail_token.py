"""One-time local helper to generate a Gmail API OAuth2 refresh token.

Usage:
    uv run --extra setup python scripts/get_gmail_token.py path/to/client_secret.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from yahoo_to_gmail_sync.gmail_client import GMAIL_SCOPES


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: uv run --extra setup python scripts/get_gmail_token.py <client_secret.json>")
        return 1

    client_secret_path = Path(sys.argv[1])
    if not client_secret_path.is_file():
        print(f"File not found: {client_secret_path}")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(GMAIL_SCOPES))
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print(
            "Error: Google did not return a refresh token. "
            "Revoke prior app access in your Google Account and run this script again."
        )
        return 1

    raw_cfg = json.loads(client_secret_path.read_text(encoding="utf-8"))
    client_info = raw_cfg.get("installed") or raw_cfg.get("web") or {}
    client_id = client_info.get("client_id", creds.client_id)
    client_secret = client_info.get("client_secret", creds.client_secret)

    secret_template = {
        "YAHOO_EMAIL": "<your-yahoo-email>",
        "YAHOO_APP_PASSWORD": "<your-yahoo-app-password>",
        "GMAIL_CLIENT_ID": client_id,
        "GMAIL_CLIENT_SECRET": client_secret,
        "GMAIL_REFRESH_TOKEN": creds.refresh_token,
    }

    print("\nStore this JSON in GCP Secret Manager (do NOT commit it):\n")
    print(json.dumps(secret_template, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
