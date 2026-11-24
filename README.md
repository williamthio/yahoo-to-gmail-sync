# yahoo-to-gmail-sync

Stateless, 100% free-forever email-forwarding pipeline running on **GitHub Actions** every 10 minutes (`*/10 * * * *`) with zero cloud billing accounts required. It fetches recent emails from Yahoo Mail over IMAP (`INBOX` only, 48-hour lookback), imports them natively into Gmail via `users.messages.import`, marks the Yahoo copy as `READ`, and archives it into a `Forwarded` folder on Yahoo.

## Architecture & Behavior

- **Runtime (100% Free Forever):**
  - Runs on a **public** GitHub repository using standard `ubuntu-latest` runners (unlimited free GitHub Actions minutes), Python 3.12 (`uv.lock` pinned), a 6-minute job timeout, non-overlapping `concurrency`, a 210s soft runtime budget, and graceful `SIGTERM` handling ([sync.yml](.github/workflows/sync.yml)).
  - Includes an automated keepalive workflow ([keepalive.yml](.github/workflows/keepalive.yml)) using the built-in `GITHUB_TOKEN` that pushes an empty commit to `main` only after 50 days of inactivity, preventing GitHub's 60-day inactivity auto-disable for scheduled workflows.
  - Requires **no GCP billing account or credit card** (only the free Gmail API + OAuth2 client on a personal Google Cloud project).
- **Source (Yahoo Mail IMAP):**
  - Connects via verified TLS (`imap.mail.yahoo.com:993`, `ssl.create_default_context()`) using a Yahoo App Password and refreshes post-auth IMAP `CAPABILITY` so `MOVE` and `UIDPLUS` are detected reliably.
  - Scans `INBOX` only for messages received within the last **48 hours** (`INTERNALDATE` UTC check).
  - Fetches messages with `BODY.PEEK[]` so messages are not marked read before Gmail import succeeds.
  - After import (or confirmed duplicate), marks `\Seen` (`READ`) and moves out of `INBOX` into `Forwarded` (auto-created if absent).
- **Destination (Gmail API):**
  - Uses OAuth2 refresh credentials and `users.messages.import` (`internalDateSource="dateHeader"`).
  - Preserves original `From`, `Date`, `Message-ID`, threading, and attachments.
  - Lands in Gmail `INBOX` as `UNREAD` with an auto-created `Yahoo` label; Gmail's spam filter remains active (`neverMarkSpam` is left at its default `False`).
- **Crash-Safe Deduplication & Failure Handling:**
  - Before calling `users.messages.import`, checks Gmail via `users.messages.list(q='rfc822msgid:"<id>"', includeSpamTrash=True)`.
  - If a run crashes after Gmail import but before Yahoo's move completes, the next run detects the existing `Message-ID`, skips re-importing, and finishes moving the Yahoo message cleanly.
  - **Unimportable messages stay in Yahoo `INBOX`:** Messages exceeding Gmail's 35 MB import limit or permanently rejected by Gmail (HTTP 400) are intentionally left untouched in Yahoo `INBOX` so no mail is silently hidden; the workflow logs the UID and exits non-zero so GitHub notifies you until the message is moved manually or ages past `SYNC_MAX_AGE_HOURS`.
- **Public-Repo Security Design:**
  - **Zero-PII UTC logs:** Emits plain-text UTC logs (`YYYY-MM-DDTHH:MM:SSZ LEVEL logger: message`) containing counts and numeric IMAP UIDs only - never email addresses, headers, bodies, OAuth tokens, or raw API/IMAP error payloads - safe for publicly readable Actions logs.
  - **5 individual GitHub Repository Secrets:** Storing each credential as its own secret lets GitHub Actions mask every individual value in logs and makes single-credential rotation straightforward.
  - **SHA-pinned, least-privilege workflows:** All GitHub Actions are pinned to immutable commit SHAs. [sync.yml](.github/workflows/sync.yml) and [ci.yml](.github/workflows/ci.yml) run with `permissions: contents: read` and `persist-credentials: false`; `sync.yml` only triggers on `schedule` and `workflow_dispatch` (never `pull_request`).

---

## Setup Guide

### 1. Generate a Yahoo App Password
1. Go to your Yahoo Account Security page.
2. Enable 2-Step Verification if it is not already enabled.
3. In Yahoo Mail settings (**More Settings -> Mailboxes**), make sure IMAP access is enabled.
4. Under **Account Security -> App passwords**, create a dedicated password for `yahoo-to-gmail-sync`.
5. Save your full Yahoo email address (`YAHOO_EMAIL`) and generated 16-character password (`YAHOO_APP_PASSWORD`).

### 2. Create Google Cloud OAuth2 Credentials & Refresh Token (No Billing Required)
1. In [Google Cloud Console](https://console.cloud.google.com/), create (or select) a project and enable the **Gmail API**. You do **not** need to attach a billing account or credit card.
2. Go to **Google Auth Platform -> Audience** (or **OAuth consent screen**):
   - Choose **External** (for personal `@gmail.com` accounts).
   - **Important:** Click **Publish App** to move it to **Production**. If left in **Testing**, Google expires refresh tokens after **7 days** (staying unverified in Production is free with a 100-user cap; you simply click through the "unverified app" warning once when authorizing your own account).
3. Go to **Clients -> Create Client -> Desktop app** and download `client_secret_*.json` (do **not** commit it; `.gitignore` ignores it).
4. Run the local helper script once to authorize the minimal scopes (`gmail.insert`, `gmail.readonly`, `gmail.labels`) and print your Gmail secrets:
   ```bash
   uv run --extra setup python scripts/get_gmail_token.py path/to/client_secret.json
   ```
5. Delete the downloaded `client_secret_*.json` file once you have added the secrets to GitHub in Step 3.

### 3. Configure GitHub Repository Secrets & Permissions
1. Keep the repository **public** so GitHub Actions `ubuntu-latest` minutes are unlimited and free. (On a private GitHub Free repository, the 2,000 min/month cap would be exceeded by a 10-minute schedule.)
2. Go to **Settings -> Secrets and variables -> Actions -> Secrets** and add these 5 **Repository secrets**:

   | Secret Name | Value |
   |---|---|
   | `YAHOO_EMAIL` | Your full Yahoo email address |
   | `YAHOO_APP_PASSWORD` | Your 16-character Yahoo App Password |
   | `GMAIL_CLIENT_ID` | Printed by `scripts/get_gmail_token.py` |
   | `GMAIL_CLIENT_SECRET` | Printed by `scripts/get_gmail_token.py` |
   | `GMAIL_REFRESH_TOKEN` | Printed by `scripts/get_gmail_token.py` |

3. Confirm `main` is not locked against `GITHUB_TOKEN` pushes so [keepalive.yml](.github/workflows/keepalive.yml) can push an empty commit once every 50 days of inactivity.
4. Go to **Actions -> Sync Yahoo to Gmail -> Run workflow** to trigger one manual run and verify end-to-end delivery.

---

## Optional Configuration Overrides

To customize defaults without modifying code, add any of these as **Repository variables** under **Settings -> Secrets and variables -> Actions -> Variables** (or export them in your local shell):

- `YAHOO_IMAP_HOST` (default: `imap.mail.yahoo.com`)
- `YAHOO_IMAP_PORT` (default: `993`)
- `YAHOO_SOURCE_FOLDER` (default: `INBOX`)
- `YAHOO_ARCHIVE_FOLDER` (default: `Forwarded`)
- `SYNC_MAX_AGE_HOURS` (default: `48`)
- `SYNC_MAX_RUNTIME_SECONDS` (default: `210`)
- `GMAIL_LABEL_NAME` (default: `Yahoo`)

## Local Development

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
