"""CLI entrypoint for scheduled and manual Cloud Run Job executions."""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime

from yahoo_to_gmail_sync.config import ConfigError, SyncConfig
from yahoo_to_gmail_sync.sync import format_safe_error, run_sync


class _CloudLoggingJsonFormatter(logging.Formatter):
    """Emit single-line JSON records parsed natively by GCP Cloud Logging."""

    converter = staticmethod(time.gmtime)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=True)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_CloudLoggingJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # Suppress verbose third-party HTTP/discovery logs.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _install_sigterm_handler(stop_event: threading.Event, logger: logging.Logger) -> None:
    """Ask the sync loop to stop cleanly when Cloud Run sends SIGTERM."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _handle_sigterm(_signum: int, _frame: object) -> None:
        logger.warning("Received SIGTERM; finishing current message before exiting")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)


def main() -> int:
    _configure_logging()
    logger = logging.getLogger(__name__)

    stop_event = threading.Event()
    _install_sigterm_handler(stop_event, logger)

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
