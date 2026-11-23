"""CLI entrypoint for scheduled and manual sync runs."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from yahoo_to_gmail_sync.config import ConfigError, SyncConfig
from yahoo_to_gmail_sync.sync import SyncStats, format_safe_error, run_sync


def _configure_logging() -> None:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    # Suppress verbose third-party HTTP/discovery logs.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _write_step_summary(stats: SyncStats, *, error_message: str | None = None) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return

    lines = ["### Yahoo to Gmail Sync Summary", ""]
    if error_message:
        lines.append(f"- **Status:** Failed ({error_message})")
    else:
        status = "Completed with errors" if stats.failed else "Success"
        lines.extend(
            [
                f"- **Status:** {status}",
                f"- **Scanned (INBOX search):** {stats.scanned}",
                f"- **Eligible (within lookback):** {stats.eligible}",
                f"- **Imported to Gmail:** {stats.imported}",
                f"- **Skipped (already in Gmail):** {stats.skipped_duplicate}",
                f"- **Skipped (older than cutoff):** {stats.skipped_too_old}",
                f"- **Failed:** {stats.failed}",
            ]
        )
        if stats.failed_uids:
            lines.append(f"- **Failed UIDs:** {', '.join(stats.failed_uids)}")

    with Path(summary_path).open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    _configure_logging()
    logger = logging.getLogger(__name__)

    try:
        config = SyncConfig.from_env()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        _write_step_summary(SyncStats(), error_message=type(exc).__name__)
        return 1

    try:
        stats = run_sync(config)
    except Exception as exc:
        safe_error = format_safe_error(exc)
        logger.error("Sync aborted: %s", safe_error)
        _write_step_summary(SyncStats(), error_message=safe_error)
        return 1

    _write_step_summary(stats)
    logger.info(
        "Sync finished: scanned=%d eligible=%d imported=%d "
        "skipped_duplicate=%d skipped_too_old=%d failed=%d failed_uids=%s",
        stats.scanned,
        stats.eligible,
        stats.imported,
        stats.skipped_duplicate,
        stats.skipped_too_old,
        stats.failed,
        ",".join(stats.failed_uids) if stats.failed_uids else "none",
    )
    return 1 if stats.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
