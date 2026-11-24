"""CLI entrypoint for scheduled and manual sync executions."""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from yahoo_to_gmail_sync.config import ConfigError, SyncConfig
from yahoo_to_gmail_sync.sync import format_safe_error, run_sync


class _UtcPlainFormatter(logging.Formatter):
    """Emit zero-PII UTC plain-text log lines for console and GitHub Actions logs."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{timestamp} {record.levelname} {record.name}: {record.getMessage()}"


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_UtcPlainFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # Suppress verbose third-party HTTP/discovery logs.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def _sigterm_stop_flag() -> Iterator[threading.Event]:
    """Install a reentrant-safe SIGTERM handler and restore the prior handler on exit."""
    stop_event = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        yield stop_event
        return

    def _handle_sigterm(_signum: int, _frame: object) -> None:
        # Keep the signal handler strictly async-safe (no logging / I/O here).
        stop_event.set()

    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        yield stop_event
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def main() -> int:
    _configure_logging()
    logger = logging.getLogger(__name__)

    with _sigterm_stop_flag() as stop_event:
        try:
            config = SyncConfig.from_env()
        except ConfigError as exc:
            logger.error("Configuration error: %s", exc)
            return 1

        try:
            stats = run_sync(config, should_stop=stop_event.is_set)
        except Exception as exc:
            logger.error("Sync aborted: %s", format_safe_error(exc))
            return 1

    logger.info(
        "Sync finished: scanned=%d eligible=%d imported=%d "
        "skipped_duplicate=%d skipped_too_old=%d deferred=%d failed=%d failed_uids=%s",
        stats.scanned,
        stats.eligible,
        stats.imported,
        stats.skipped_duplicate,
        stats.skipped_too_old,
        stats.deferred,
        stats.failed,
        ",".join(stats.failed_uids) if stats.failed_uids else "none",
    )
    return 1 if stats.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
