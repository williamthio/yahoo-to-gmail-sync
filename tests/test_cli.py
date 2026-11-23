"""Unit tests for the CLI entrypoint and Cloud Logging JSON output."""

from __future__ import annotations

import json
import logging
import signal
import time

from yahoo_to_gmail_sync import cli
from yahoo_to_gmail_sync.gmail_client import GmailApiError
from yahoo_to_gmail_sync.sync import SyncStats


def _parse_json_lines(text: str) -> list[dict[str, str]]:
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


def test_cli_logs_json_error_to_stdout_without_mutating_global_converter(
    monkeypatch, capsys
) -> None:
    original_converter = logging.Formatter.converter
    for key in (
        "SYNC_CREDENTIALS_JSON",
        "YAHOO_EMAIL",
        "YAHOO_APP_PASSWORD",
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    records = _parse_json_lines(captured.out)
    assert len(records) == 1
    assert records[0]["severity"] == "ERROR"
    assert "Configuration error" in records[0]["message"]
    assert records[0]["timestamp"].endswith("Z")
    assert logging.Formatter.converter is original_converter
    assert original_converter is time.localtime


def test_cli_logs_info_summary_and_returns_expected_exit_codes(monkeypatch, capsys) -> None:
    monkeypatch.setenv("YAHOO_EMAIL", "u@yahoo.com")
    monkeypatch.setenv("YAHOO_APP_PASSWORD", "pw")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rt")

    monkeypatch.setattr(
        cli,
        "run_sync",
        lambda _cfg, **_kwargs: SyncStats(
            scanned=3,
            eligible=2,
            imported=1,
            skipped_duplicate=1,
            skipped_too_old=1,
            failed=0,
        ),
    )
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    records = _parse_json_lines(captured.out)
    assert records[-1]["severity"] == "INFO"
    assert "imported=1" in records[-1]["message"]
    assert "failed_uids=none" in records[-1]["message"]

    monkeypatch.setattr(
        cli,
        "run_sync",
        lambda _cfg, **_kwargs: SyncStats(
            scanned=1,
            eligible=1,
            imported=0,
            failed=1,
            failed_uids=["99"],
        ),
    )
    assert cli.main() == 1
    captured = capsys.readouterr()
    records = _parse_json_lines(captured.out)
    assert records[-1]["severity"] == "INFO"
    assert "failed=1" in records[-1]["message"]
    assert "failed_uids=99" in records[-1]["message"]


def test_cli_surfaces_sanitized_api_error_on_abort(monkeypatch, capsys) -> None:
    monkeypatch.setenv("YAHOO_EMAIL", "u@yahoo.com")
    monkeypatch.setenv("YAHOO_APP_PASSWORD", "pw")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rt")

    def _boom(_cfg, **_kwargs):
        raise GmailApiError("Gmail API labels.list failed with HTTP 401", status_code=401)

    monkeypatch.setattr(cli, "run_sync", _boom)
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    records = _parse_json_lines(captured.out)
    assert records[-1]["severity"] == "ERROR"
    assert "Gmail API labels.list failed with HTTP 401" in records[-1]["message"]


def test_sigterm_handler_sets_flag_without_logging_and_restores_previous(capsys) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    with cli._sigterm_stop_flag() as stop_event:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert stop_event.is_set() is False
        handler(signal.SIGTERM, None)
        assert stop_event.is_set() is True

    assert signal.getsignal(signal.SIGTERM) is previous
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
