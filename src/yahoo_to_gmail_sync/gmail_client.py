"""Gmail API client for deduplicated RFC 822 message import."""

from __future__ import annotations

import io
import logging
import random
import time
from collections.abc import Callable
from email import policy
from email.parser import BytesHeaderParser
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
)
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105

MAX_IMPORT_BYTES = 35 * 1024 * 1024
_RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024
_MAX_RETRIES = 4
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


class GmailApiError(RuntimeError):
    """Sanitized Gmail API error that strips raw response bodies and headers."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def extract_message_id(raw_bytes: bytes) -> str | None:
    """Extract and normalize the RFC 5322 Message-ID header, if present."""
    headers = BytesHeaderParser(policy=policy.default).parsebytes(raw_bytes)
    raw_id = headers.get("Message-ID")
    if not raw_id:
        return None
    normalized = " ".join(str(raw_id).split()).strip()
    return normalized or None


class GmailApiClient:
    """Minimal, zero-PII Gmail API wrapper using OAuth2 refresh credentials."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        service: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep_fn
        if service is not None:
            self._service = service
        else:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=GOOGLE_TOKEN_URI,
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(GMAIL_SCOPES),
            )
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _execute_with_retry(self, operation_name: str, request_factory: Callable[[], Any]) -> Any:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return request_factory().execute()
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                retryable = status in _RETRYABLE_HTTP_STATUSES
                if not retryable or attempt == _MAX_RETRIES:
                    raise GmailApiError(
                        f"Gmail API {operation_name} failed with HTTP {status}",
                        status_code=status,
                        retryable=retryable,
                    ) from exc
                delay = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)  # noqa: S311
                logger.warning(
                    "Retrying Gmail API %s after HTTP %s (attempt %d/%d, %.2fs)",
                    operation_name,
                    status,
                    attempt,
                    _MAX_RETRIES,
                    delay,
                )
                self._sleep(delay)
            except (OSError, TimeoutError) as exc:
                if attempt == _MAX_RETRIES:
                    raise GmailApiError(
                        f"Gmail API {operation_name} network error ({type(exc).__name__})",
                        retryable=True,
                    ) from exc
                delay = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)  # noqa: S311
                logger.warning(
                    "Retrying Gmail API %s after %s (attempt %d/%d, %.2fs)",
                    operation_name,
                    type(exc).__name__,
                    attempt,
                    _MAX_RETRIES,
                    delay,
                )
                self._sleep(delay)
            except Exception as exc:
                raise GmailApiError(
                    f"Gmail API {operation_name} error ({type(exc).__name__})",
                    retryable=False,
                ) from exc
        raise GmailApiError(f"Gmail API {operation_name} exhausted retries", retryable=True)

    def ensure_label(self, label_name: str) -> str:
        """Return the ID of an existing Gmail label or create it if absent."""
        resp = self._execute_with_retry(
            "labels.list",
            lambda: self._service.users().labels().list(userId="me"),
        )
        labels = resp.get("labels", []) if isinstance(resp, dict) else []

        for label in labels:
            if label.get("name") == label_name:
                return str(label["id"])
        for label in labels:
            if str(label.get("name", "")).lower() == label_name.lower():
                return str(label["id"])

        created = self._execute_with_retry(
            "labels.create",
            lambda: (
                self._service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": label_name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
            ),
        )
        return str(created["id"])

    def message_exists(self, message_id: str) -> bool:
        """Check whether a message with the given RFC 5322 Message-ID already exists in Gmail."""
        # Strip surrounding whitespace and avoid breaking the Gmail search operator.
        sanitized_id = message_id.replace('"', "").strip()
        if not sanitized_id:
            return False

        query = f"rfc822msgid:{sanitized_id}"
        resp = self._execute_with_retry(
            "messages.list",
            lambda: (
                self._service.users()
                .messages()
                .list(userId="me", q=query, includeSpamTrash=True, maxResults=1)
            ),
        )
        messages = resp.get("messages", []) if isinstance(resp, dict) else []
        return bool(messages)

    def import_message(self, raw_bytes: bytes, label_ids: list[str]) -> str:
        """Import a raw RFC 822 message into Gmail with the requested labels."""
        if len(raw_bytes) > MAX_IMPORT_BYTES:
            raise GmailApiError("Message exceeds Gmail 35 MB import limit", retryable=False)

        use_resumable = len(raw_bytes) > _RESUMABLE_THRESHOLD_BYTES

        def _build_request() -> Any:
            media = MediaIoBaseUpload(
                io.BytesIO(raw_bytes),
                mimetype="message/rfc822",
                resumable=use_resumable,
            )
            return (
                self._service.users()
                .messages()
                .import_(
                    userId="me",
                    body={"labelIds": label_ids},
                    media_body=media,
                    internalDateSource="dateHeader",
                )
            )

        resp = self._execute_with_retry("messages.import", _build_request)
        return str(resp.get("id", ""))
