"""Unit tests for the CLI entrypoint, UTC logging, and summary output."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from yahoo_to_gmail_sync import cli
from yahoo_to_gmail_sync.gmail_client import GmailApiError
from yahoo_to_gmail_sync.sync import SyncStats


def test_cli_returns_nonzero_on_missing_env(monkeypatch, tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for key in (
        "SYNC_CREDENTIALS_JSON",
        "YAHOO_EMAIL",
        "YAHOO_APP_PASSWORD",
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    exit_code = cli.main()
    assert exit_code == 1
    assert "Failed (ConfigError)" in summary.read_text(encoding="utf-8")
    assert logging.Formatter.converter is time.gmtime


def test_cli_writes_summary_with_failed_uids_and_returns_exit_code(
    monkeypatch, tmp_path: Path
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("YAHOO_EMAIL", "u@yahoo.com")
    monkeypatch.setenv("YAHOO_APP_PASSWORD", "pw")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rt")

    monkeypatch.setattr(
        cli,
        "run_sync",
        lambda _cfg: SyncStats(
            scanned=3,
            eligible=2,
            imported=1,
            skipped_duplicate=1,
            skipped_too_old=1,
            failed=0,
        ),
    )
    assert cli.main() == 0
    text = summary.read_text(encoding="utf-8")
    assert "**Status:** Success" in text
    assert "**Imported to Gmail:** 1" in text

    monkeypatch.setattr(
        cli,
        "run_sync",
        lambda _cfg: SyncStats(
            scanned=1,
            eligible=1,
            imported=0,
            failed=1,
            failed_uids=["99"],
        ),
    )
    assert cli.main() == 1
    updated = summary.read_text(encoding="utf-8")
    assert "Completed with errors" in updated
    assert "**Failed UIDs:** 99" in updated


def test_cli_surfaces_sanitized_api_error_on_abort(monkeypatch, tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("YAHOO_EMAIL", "u@yahoo.com")
    monkeypatch.setenv("YAHOO_APP_PASSWORD", "pw")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rt")

    def _boom(_cfg):
        raise GmailApiError("Gmail API labels.list failed with HTTP 401", status_code=401)

    monkeypatch.setattr(cli, "run_sync", _boom)
    assert cli.main() == 1
    assert "Failed (Gmail API labels.list failed with HTTP 401)" in summary.read_text(
        encoding="utf-8"
    )
