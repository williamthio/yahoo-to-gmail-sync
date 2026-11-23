"""Core stateless email synchronization pipeline."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from yahoo_to_gmail_sync.config import SyncConfig
from yahoo_to_gmail_sync.gmail_client import (
    MAX_IMPORT_BYTES,
    GmailApiClient,
    GmailApiError,
    extract_message_id,
)
from yahoo_to_gmail_sync.yahoo_client import YahooCandidate, YahooImapClient, YahooImapError

logger = logging.getLogger(__name__)


class YahooClientProtocol(Protocol):
    def __enter__(self) -> YahooClientProtocol: ...
    def __exit__(self, *args: object) -> None: ...
    def ensure_archive_folder(self) -> None: ...
    def list_recent_candidates(
        self, cutoff_utc: datetime
    ) -> tuple[list[YahooCandidate], int, int]: ...
    def fetch_raw_message(self, uid: str) -> bytes: ...
    def mark_read_and_move(self, uid: str) -> None: ...


class GmailClientProtocol(Protocol):
    def ensure_label(self, label_name: str) -> str: ...
    def message_exists(self, message_id: str) -> bool: ...
    def import_message(self, raw_bytes: bytes, label_ids: list[str]) -> str: ...


@dataclass(slots=True)
class SyncStats:
    """Non-PII run statistics."""

    scanned: int = 0
    eligible: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    skipped_too_old: int = 0
    failed: int = 0
    deferred: int = 0
    failed_uids: list[str] = field(default_factory=list)


def format_safe_error(exc: Exception) -> str:
    """Return a zero-PII error description suitable for logs."""
    if isinstance(exc, (GmailApiError, YahooImapError)):
        return str(exc)
    return type(exc).__name__


def run_sync(
    config: SyncConfig,
    *,
    yahoo_client: YahooClientProtocol | None = None,
    gmail_client: GmailClientProtocol | None = None,
    now_utc: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> SyncStats:
    """Execute one stateless sync pass from Yahoo INBOX to Gmail."""
    deadline = monotonic_fn() + config.max_runtime_seconds
    current_time = now_utc or datetime.now(UTC)
    cutoff_utc = current_time - timedelta(hours=config.max_age_hours)

    yahoo: YahooClientProtocol = yahoo_client or YahooImapClient(
        config.yahoo_email,
        config.yahoo_app_password,
        host=config.yahoo_imap_host,
        port=config.yahoo_imap_port,
        source_folder=config.yahoo_source_folder,
        archive_folder=config.yahoo_archive_folder,
    )
    gmail: GmailClientProtocol = gmail_client or GmailApiClient(
        config.gmail_client_id,
        config.gmail_client_secret,
        config.gmail_refresh_token,
    )

    stats = SyncStats()

    # Validate Gmail OAuth credentials and resolve target label up front on every run.
    yahoo_label_id = gmail.ensure_label(config.gmail_label_name)
    target_labels = ["INBOX", "UNREAD", yahoo_label_id]

    with yahoo:
        candidates, total_scanned, skipped_too_old = yahoo.list_recent_candidates(cutoff_utc)
        stats.scanned = total_scanned
        stats.skipped_too_old = skipped_too_old
        stats.eligible = len(candidates)

        logger.info(
            "Scan complete: scanned=%d eligible=%d skipped_too_old=%d",
            stats.scanned,
            stats.eligible,
            stats.skipped_too_old,
        )

        if not candidates:
            return stats

        yahoo.ensure_archive_folder()

        for index, candidate in enumerate(candidates):
            if (should_stop is not None and should_stop()) or monotonic_fn() >= deadline:
                stats.deferred = len(candidates) - index
                logger.warning(
                    "Stopping early due to shutdown signal or time budget; "
                    "deferring %d remaining candidates to next run",
                    stats.deferred,
                )
                break

            if candidate.size_bytes > MAX_IMPORT_BYTES:
                logger.error(
                    "Skipping UID %s: size_bytes=%d exceeds Gmail 35 MB import limit",
                    candidate.uid,
                    candidate.size_bytes,
                )
                stats.failed += 1
                stats.failed_uids.append(candidate.uid)
                continue

            try:
                raw_bytes = yahoo.fetch_raw_message(candidate.uid)
                message_id = extract_message_id(raw_bytes)

                if message_id is not None and gmail.message_exists(message_id):
                    yahoo.mark_read_and_move(candidate.uid)
                    stats.skipped_duplicate += 1
                    logger.info("UID %s already present in Gmail; moved on Yahoo", candidate.uid)
                    continue

                gmail.import_message(raw_bytes, target_labels)
                yahoo.mark_read_and_move(candidate.uid)
                stats.imported += 1
                logger.info("UID %s imported to Gmail and moved on Yahoo", candidate.uid)
            except Exception as exc:
                stats.failed += 1
                stats.failed_uids.append(candidate.uid)
                logger.error("Failed processing UID %s: %s", candidate.uid, format_safe_error(exc))

    return stats
