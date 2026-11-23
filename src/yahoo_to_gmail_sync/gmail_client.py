"""Gmail API client for deduplicated RFC 822 message import."""

from __future__ import annotations

import http.client
import io
import logging
import random
import re
import time
from collections.abc import Callable
from email import policy
from email.parser import BytesHeaderParser
from typing import Any

import httplib2
from google.auth.exceptions import TransportError
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
RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024
RESUMABLE_CHUNK_BYTES = 5 * 1024 * 1024

_MAX_RETRIES = 4
_IDEMPOTENT_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_NON_IDEMPOTENT_RETRYABLE_STATUSES = frozenset({429})
_TRANSPORT_ERRORS = (
    OSError,
    TimeoutError,
    httplib2.HttpLib2Error,
    http.client.HTTPException,
    TransportError,
)
_ANGLE_MSG_ID_PATTERN = re.compile(r"<[^<>\r\n]+>")
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")


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


def _slice_header_block(raw_bytes: bytes) -> bytes:
    """Return only the RFC 5322 header section to avoid buffering large bodies in memory."""
    crlf_idx = raw_bytes.find(b"\r\n\r\n")
    lf_idx = raw_bytes.find(b"\n\n")
    candidates = [idx for idx in (crlf_idx, lf_idx) if idx != -1]
    if not candidates:
        return raw_bytes
    return raw_bytes[: min(candidates)]


def extract_message_id(raw_bytes: bytes) -> str | None:
    """Extract and normalize the RFC 5322 Message-ID header, if present."""
    header_bytes = _slice_header_block(raw_bytes)
    headers = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes)
    raw_id = headers.get("Message-ID")
    if not raw_id:
        return None
    normalized = " ".join(str(raw_id).split()).strip()
    if not normalized:
        return None

    # Prefer the angle-bracketed msg-id token so RFC 5322 comments are dropped cleanly.
    bracketed = _ANGLE_MSG_ID_PATTERN.search(normalized)
    if bracketed:
        return bracketed.group(0)
    return normalized


def _escape_gmail_quoted_term(value: str) -> str:
    """Escape a value for safe interpolation inside a double-quoted Gmail search term."""
    cleaned = _CONTROL_CHARS_PATTERN.sub("", value).strip()
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


class GmailApiClient:
    """Minimal, zero-PII Gmail API wrapper using OAuth2 refresh credentials."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        service: Any | None = None,
        service_factory: Callable[[], Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep_fn
        self._service: Any | None = service

        if service_factory is not None:
            self._service_factory = service_factory
        else:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=GOOGLE_TOKEN_URI,
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(GMAIL_SCOPES),
            )
            self._service_factory = lambda: build(
                "gmail", "v1", credentials=creds, cache_discovery=False
            )

    def _backoff(self, attempt: int) -> float:
        return (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)  # noqa: S311

    def _run_with_retry(
        self,
        operation_name: str,
        fn: Callable[[], Any],
        *,
        idempotent: bool = True,
    ) -> Any:
        retryable_statuses = (
            _IDEMPOTENT_RETRYABLE_STATUSES if idempotent else _NON_IDEMPOTENT_RETRYABLE_STATUSES
        )

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return fn()
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                retryable = status in retryable_statuses
                if not retryable or attempt == _MAX_RETRIES:
                    raise GmailApiError(
                        f"Gmail API {operation_name} failed with HTTP {status}",
                        status_code=status,
                        retryable=status in _IDEMPOTENT_RETRYABLE_STATUSES,
                    ) from exc
                delay = self._backoff(attempt)
                logger.warning(
                    "Retrying Gmail API %s after HTTP %s (attempt %d/%d, %.2fs)",
                    operation_name,
                    status,
                    attempt,
                    _MAX_RETRIES,
                    delay,
                )
                self._sleep(delay)
            except _TRANSPORT_ERRORS as exc:
                if not idempotent or attempt == _MAX_RETRIES:
                    raise GmailApiError(
                        f"Gmail API {operation_name} network error ({type(exc).__name__})",
                        retryable=True,
                    ) from exc
                delay = self._backoff(attempt)
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

    def _get_service(self) -> Any:
        """Lazily initialize the Gmail discovery client with transient-error retries."""
        if self._service is None:
            self._service = self._run_with_retry(
                "discovery.build", self._service_factory, idempotent=True
            )
        return self._service

    def _execute_with_retry(
        self,
        operation_name: str,
        request_factory: Callable[[Any], Any],
        *,
        idempotent: bool = True,
    ) -> Any:
        service = self._get_service()
        return self._run_with_retry(
            operation_name,
            lambda: request_factory(service).execute(),
            idempotent=idempotent,
        )

    def _find_label_id(self, label_name: str) -> str | None:
        resp = self._execute_with_retry(
            "labels.list",
            lambda svc: svc.users().labels().list(userId="me"),
            idempotent=True,
        )
        labels = resp.get("labels", []) if isinstance(resp, dict) else []

        for label in labels:
            if label.get("name") == label_name:
                return str(label["id"])
        for label in labels:
            if str(label.get("name", "")).lower() == label_name.lower():
                return str(label["id"])
        return None

    def ensure_label(self, label_name: str) -> str:
        """Return the ID of an existing Gmail label or create it if absent."""
        existing = self._find_label_id(label_name)
        if existing is not None:
            return existing

        try:
            created = self._execute_with_retry(
                "labels.create",
                lambda svc: (
                    svc.users()
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
                idempotent=False,
            )
            return str(created["id"])
        except GmailApiError as exc:
            # Handle race condition when overlapping runs create the label concurrently.
            if exc.status_code == 409:
                rechecked = self._find_label_id(label_name)
                if rechecked is not None:
                    return rechecked
            raise

    def message_exists(self, message_id: str) -> bool:
        """Check whether a message with the given RFC 5322 Message-ID already exists in Gmail."""
        escaped_id = _escape_gmail_quoted_term(message_id)
        if not escaped_id:
            return False

        # Quote and escape the operand so spaces, quotes, or trailing backslashes stay bounded.
        query = f'rfc822msgid:"{escaped_id}"'
        resp = self._execute_with_retry(
            "messages.list",
            lambda svc: (
                svc.users()
                .messages()
                .list(userId="me", q=query, includeSpamTrash=True, maxResults=1)
            ),
            idempotent=True,
        )
        messages = resp.get("messages", []) if isinstance(resp, dict) else []
        return bool(messages)

    def import_message(self, raw_bytes: bytes, label_ids: list[str]) -> str:
        """Import a raw RFC 822 message into Gmail with the requested labels."""
        if len(raw_bytes) > MAX_IMPORT_BYTES:
            raise GmailApiError("Message exceeds Gmail 35 MB import limit", retryable=False)

        use_resumable = len(raw_bytes) > RESUMABLE_THRESHOLD_BYTES

        def _build_request(svc: Any) -> Any:
            media = MediaIoBaseUpload(
                io.BytesIO(raw_bytes),
                mimetype="message/rfc822",
                chunksize=RESUMABLE_CHUNK_BYTES,
                resumable=use_resumable,
            )
            return (
                svc.users()
                .messages()
                .import_(
                    userId="me",
                    body={"labelIds": label_ids},
                    media_body=media,
                    internalDateSource="dateHeader",
                )
            )

        # Only retry definite rate limits (429); ambiguous 5xx/timeouts are re-checked on next run.
        resp = self._execute_with_retry("messages.import", _build_request, idempotent=False)
        return str(resp.get("id", ""))
