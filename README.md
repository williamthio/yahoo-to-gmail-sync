# yahoo-to-gmail-sync

Serverless, stateless email-forwarding pipeline running on GitHub Actions every 5 minutes (`*/5 * * * *`). It fetches recent emails from Yahoo Mail over IMAP (`INBOX` only, 48-hour lookback), imports them natively into Gmail via `users.messages.import`, marks the Yahoo copy as `READ`, and archives it into a `Forwarded` folder on Yahoo.

## Architecture & Behavior

- **Runtime:** Python 3.12+ managed with `uv` (`uv.lock` pinned).
- **Source (Yahoo Mail IMAP):**
  - Connects via TLS (`imap.mail.yahoo.com:993`) using a Yahoo App Password.
  - Scans `INBOX` only for messages received within the last **48 hours** (`INTERNALDATE` UTC check).
  - Fetches messages with `BODY.PEEK[]` so messages are not marked read before Gmail import succeeds.
  - After import (or confirmed duplicate), marks `\Seen` (`READ`) and moves out of `INBOX` into `Forwarded` (auto-created if absent).
- **Destination (Gmail API):**
  - Uses OAuth2 refresh credentials and `users.messages.import` (`internalDateSource="dateHeader"`).
  - Preserves original `From`, `Date`, `Message-ID`, threading, and attachments.
  - Lands in Gmail `INBOX` as `UNREAD` with an auto-created `Yahoo` label; Gmail's spam filter remains active (`neverMarkSpam` is left at its default `False`).
- **Crash-Safe Deduplication:**
  - Before calling `users.messages.import`, checks Gmail via `users.messages.list(q="rfc822msgid:<id>", includeSpamTrash=True)`.
  - If a run crashes after Gmail import but before Yahoo's move completes, the next run detects the existing `Message-ID`, skips re-importing, and finishes moving the Yahoo message cleanly.
- **Public-Repo Security Controls:**
  - **Zero-PII logs:** Never logs email addresses, `From`/`To`/`Subject`/`Message-ID`, bodies, OAuth tokens, or raw API/IMAP error payloads. Logs and `GITHUB_STEP_SUMMARY` contain counts and numeric IMAP UIDs only.
  - **Isolated workflows:** `sync.yml` uses `permissions: contents: read` and runs only on `schedule` / `workflow_dispatch` (never `pull_request_target`). `keepalive.yml` uses `permissions: contents: write` to prevent GitHub's 60-day schedule auto-disable when inactive > 45 days and has zero access to email secrets. `ci.yml` runs mocked unit tests with zero secrets.
  - **Supply-chain pinning:** All GitHub Actions are pinned to immutable commit SHAs, and Python packages install strictly from `uv.lock` (`--locked`).

---

## Setup Guide

### 1. Generate a Yahoo App Password
1. Go to your Yahoo Account Security page.
2. Enable 2-Step Verification if it is not already enabled.
3. Under **App passwords** (or **Generate app password**), create a dedicated password for `yahoo-to-gmail-sync`.
4. Save your full Yahoo email address (`YAHOO_EMAIL`) and the generated password (`YAHOO_APP_PASSWORD`).

### 2. Create Google Cloud OAuth2 Credentials & Refresh Token
1. In [Google Cloud Console](https://console.cloud.google.com/), create (or select) a project and enable the **Gmail API**.
2. Go to **Google Auth Platform -> Audience** (or **OAuth consent screen**):
   - Choose **External** (for personal `@gmail.com` accounts).
   - Add your Gmail address under **Test users** if leaving in Testing, **OR click "Publish App" to move it to Production**.
   - **Important:** If the OAuth consent screen stays in **Testing** mode, Google expires refresh tokens after **7 days**. Publishing to **Production** keeps your refresh token long-lived (you can click through the "unverified app" warning once when authorizing your own personal account).
3. Go to **Clients -> Create Client -> Desktop app** and download the `client_secret_*.json` file (do **not** commit this file; `.gitignore` ignores it).
4. Run the local helper script once to authorize the minimal scopes (`gmail.insert`, `gmail.readonly`, `gmail.labels`) and print your credentials:
   ```bash
   uv run --extra setup python scripts/get_gmail_token.py path/to/client_secret.json
   ```

### 3. Configure GitHub Actions Secrets
In your GitHub repository, open **Settings -> Secrets and variables -> Actions -> New repository secret** and add:

| Secret Name | Value |
|---|---|
| `YAHOO_EMAIL` | Your full Yahoo Mail address |
| `YAHOO_APP_PASSWORD` | Yahoo App Password from Step 1 |
| `GMAIL_CLIENT_ID` | OAuth Client ID from Step 2 |
| `GMAIL_CLIENT_SECRET` | OAuth Client Secret from Step 2 |
| `GMAIL_REFRESH_TOKEN` | Refresh token printed by `scripts/get_gmail_token.py` |

### 4. Verify Repository Settings for `keepalive.yml`
- Under **Settings -> Actions -> General -> Workflow permissions**, ensure workflows are permitted to use `contents: write` when requested by `keepalive.yml`.
- If you enable branch protection / rulesets on `main`, allow **GitHub Actions** in the bypass list so `keepalive.yml` can push empty commits after 45 days of inactivity.
- Trigger **Yahoo to Gmail Sync** once manually from the **Actions** tab (`Run workflow`) to verify end-to-end delivery.

---

## Optional Environment Overrides
You can customize defaults via workflow `env` or repository variables if needed:

- `YAHOO_IMAP_HOST` (default: `imap.mail.yahoo.com`)
- `YAHOO_IMAP_PORT` (default: `993`)
- `YAHOO_SOURCE_FOLDER` (default: `INBOX`)
- `YAHOO_ARCHIVE_FOLDER` (default: `Forwarded`)
- `SYNC_MAX_AGE_HOURS` (default: `48`)
- `GMAIL_LABEL_NAME` (default: `Yahoo`)

## Local Development
```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
