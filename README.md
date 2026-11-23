# yahoo-to-gmail-sync

Serverless, stateless email-forwarding pipeline running as a **GCP Cloud Run Job** triggered every 5 minutes by **Cloud Scheduler** (`*/5 * * * *`). It fetches recent emails from Yahoo Mail over IMAP (`INBOX` only, 48-hour lookback), imports them natively into Gmail via `users.messages.import`, marks the Yahoo copy as `READ`, and archives it into a `Forwarded` folder on Yahoo.

## Architecture & Behavior

- **Runtime:** Python 3.12 container (`Dockerfile` + `uv.lock` pinned), running as a Cloud Run Job (`--task-timeout=4m`, `--max-retries=0`) with a 210s soft runtime budget and graceful `SIGTERM` handling to minimize overlap between scheduled runs and emit clean summary logs before timeout.
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
  - **Unimportable messages stay in Yahoo `INBOX`:** Messages exceeding Gmail's 35 MB import limit or permanently rejected by Gmail (HTTP 400) are intentionally left untouched in Yahoo `INBOX` so no mail is silently hidden; the job logs the UID and exits non-zero on each run until the message is moved manually or ages past `SYNC_MAX_AGE_HOURS`.
- **Security & Free-Tier Design:**
  - **Single Secret Manager JSON secret (`SYNC_CREDENTIALS_JSON`):** Keeps secret accesses (~8,640/month) within Secret Manager's 10,000 free monthly accesses.
  - **Zero-PII structured logs:** Emits single-line JSON logs to `stdout` (natively parsed with accurate severities by Cloud Logging) containing counts and numeric IMAP UIDs only - never email addresses, headers, bodies, OAuth tokens, or raw API/IMAP error payloads.
  - **Keyless, SHA-pinned CI/CD:** GitHub Actions runs lint/tests/Docker smoke tests on PRs ([ci.yml](.github/workflows/ci.yml)) and deploys to Cloud Run on `main` via Workload Identity Federation locked to `refs/heads/main` ([deploy.yml](.github/workflows/deploy.yml)) with all Actions pinned to immutable commit SHAs.

---

## Setup Guide

### 1. Generate a Yahoo App Password
1. Go to your Yahoo Account Security page.
2. Enable 2-Step Verification if it is not already enabled.
3. Under **App passwords**, create a dedicated password for `yahoo-to-gmail-sync`.
4. Save your full Yahoo email address (`YAHOO_EMAIL`) and generated password (`YAHOO_APP_PASSWORD`).

### 2. Create Google Cloud OAuth2 Credentials & Refresh Token
1. In [Google Cloud Console](https://console.cloud.google.com/), create (or select) a project and enable the **Gmail API**.
2. Go to **Google Auth Platform -> Audience** (or **OAuth consent screen**):
   - Choose **External** (for personal `@gmail.com` accounts).
   - **Important:** Click **Publish App** to move it to **Production**. If left in **Testing**, Google expires refresh tokens after **7 days** (you can click through the "unverified app" warning once when authorizing your own account).
3. Go to **Clients -> Create Client -> Desktop app** and download `client_secret_*.json` (do **not** commit it; `.gitignore` ignores it).
4. Run the local helper script once to authorize the minimal scopes (`gmail.insert`, `gmail.readonly`, `gmail.labels`) and generate the Secret Manager JSON template:
   ```bash
   uv run --extra setup python scripts/get_gmail_token.py path/to/client_secret.json
   ```
5. Fill in `YAHOO_EMAIL` and `YAHOO_APP_PASSWORD` in a temporary local file `credentials.json` (ignored by `.gitignore` and deleted after Step 3).

### 3. One-Time GCP Bootstrap (Cloud Run Job + Cloud Scheduler)

Set your variables (pick a Tier 1 Cloud Run region such as `us-central1` or `asia-east1`):

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export REPO_NAME="yahoo-to-gmail-sync"
export JOB_NAME="yahoo-to-gmail-sync"
export SECRET_NAME="yahoo-to-gmail-sync-creds"
export GITHUB_REPO="your-github-user/yahoo-to-gmail-sync"

gcloud config set project "$PROJECT_ID"
```

1. **Enable required APIs:**
   ```bash
   gcloud services enable \
     run.googleapis.com \
     cloudscheduler.googleapis.com \
     artifactregistry.googleapis.com \
     secretmanager.googleapis.com \
     iamcredentials.googleapis.com \
     gmail.googleapis.com
   ```

2. **Create Artifact Registry repository with a Delete + Keep-Latest-5 cleanup policy** (keeps storage under the 0.5 GB free tier):
   ```bash
   gcloud artifacts repositories create "$REPO_NAME" \
     --repository-format=docker \
     --location="$REGION"

   cat <<'EOF' > /tmp/ar-cleanup-policy.json
   [
     {
       "name": "delete-old",
       "action": {"type": "Delete"},
       "condition": {"tagState": "any"}
     },
     {
       "name": "keep-latest-5",
       "action": {"type": "Keep"},
       "mostRecentVersions": {"keepCount": 5}
     }
   ]
   EOF

   gcloud artifacts repositories set-cleanup-policies "$REPO_NAME" \
     --location="$REGION" \
     --policy=/tmp/ar-cleanup-policy.json
   rm /tmp/ar-cleanup-policy.json
   ```

3. **Store credentials as 1 Secret Manager JSON secret** (then delete the local file):
   ```bash
   gcloud secrets create "$SECRET_NAME" --replication-policy=automatic
   gcloud secrets versions add "$SECRET_NAME" --data-file=credentials.json
   rm credentials.json
   ```

4. **Create least-privilege service accounts for runtime and scheduler:**
   ```bash
   gcloud iam service-accounts create sync-job-sa --display-name="Yahoo-Gmail Sync Job"
   gcloud iam service-accounts create sync-scheduler-sa --display-name="Yahoo-Gmail Sync Scheduler"

   gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
     --member="serviceAccount:sync-job-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

5. **Build and push the initial container image, then create the Cloud Run Job:**
   ```bash
   export IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:initial"
   gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
   docker build --platform linux/amd64 --tag "$IMAGE_URI" .
   docker push "$IMAGE_URI"

   gcloud run jobs create "$JOB_NAME" \
     --region="$REGION" \
     --image="$IMAGE_URI" \
     --service-account="sync-job-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --set-secrets="SYNC_CREDENTIALS_JSON=${SECRET_NAME}:latest" \
     --cpu=1 \
     --memory=512Mi \
     --tasks=1 \
     --max-retries=0 \
     --task-timeout=4m
   ```

6. **Run one manual execution to verify end-to-end delivery:**
   ```bash
   gcloud run jobs execute "$JOB_NAME" --region="$REGION" --wait
   ```

7. **Create the 5-minute Cloud Scheduler trigger:**
   ```bash
   gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
     --region="$REGION" \
     --member="serviceAccount:sync-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/run.invoker"

   gcloud scheduler jobs create http "${JOB_NAME}-schedule" \
     --location="$REGION" \
     --schedule="*/5 * * * *" \
     --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
     --http-method=POST \
     --oauth-service-account-email="sync-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"
   ```

### 4. Configure GitHub Actions CD (Workload Identity Federation)

1. **Create a deployer service account and WIF pool/provider restricted to `refs/heads/main`:**
   ```bash
   export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

   gcloud iam service-accounts create github-deployer-sa --display-name="GitHub Actions Deployer"

   gcloud artifacts repositories add-iam-policy-binding "$REPO_NAME" \
     --location="$REGION" \
     --member="serviceAccount:github-deployer-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/artifactregistry.writer"

   gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
     --region="$REGION" \
     --member="serviceAccount:github-deployer-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/run.developer"

   gcloud iam service-accounts add-iam-policy-binding "sync-job-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --member="serviceAccount:github-deployer-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/iam.serviceAccountUser"

   gcloud iam workload-identity-pools create github-pool \
     --location="global" \
     --display-name="GitHub Actions Pool"

   gcloud iam workload-identity-pools providers create-oidc github-provider \
     --location="global" \
     --workload-identity-pool="github-pool" \
     --issuer-uri="https://token.actions.githubusercontent.com" \
     --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
     --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main'"

   gcloud iam service-accounts add-iam-policy-binding "github-deployer-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${GITHUB_REPO}"
   ```

2. **Add Repository Variables in GitHub** (**Settings -> Secrets and variables -> Actions -> Variables**):

   | Variable Name | Value |
   |---|---|
   | `GCP_PROJECT_ID` | Your GCP project ID |
   | `GCP_REGION` | Your GCP region (e.g. `us-central1`) |
   | `GCP_WIF_PROVIDER` | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
   | `GCP_DEPLOYER_SA` | `github-deployer-sa@<PROJECT_ID>.iam.gserviceaccount.com` |

   After this, every push to `main` automatically runs tests, builds/pushes a new container image, and updates the Cloud Run Job.

---

## Optional Environment Overrides

Set these via `gcloud run jobs update "$JOB_NAME" --region="$REGION" --update-env-vars=...` (or locally in your shell):

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
