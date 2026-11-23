"""Unit tests for the Yahoo IMAP client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from yahoo_to_gmail_sync.yahoo_client import (
    YahooImapClient,
    YahooImapError,
    parse_imap_internaldate,
)


def test_parse_imap_internaldate_converts_to_utc() -> None:
    dt = parse_imap_internaldate(" 2-Nov-2026 10:00:00 +0700")
    assert dt == datetime(2026, 11, 2, 3, 0, 0, tzinfo=UTC)


def test_list_recent_candidates_filters_by_cutoff() -> None:
    fake_conn = MagicMock()
    fake_conn.login.return_value = ("OK", [b"Logged in"])
    fake_conn.select.return_value = ("OK", [b"2"])
    fake_conn.uid.side_effect = [
        ("OK", [b"10 11"]),
        (
            "OK",
            [
                b'1 (UID 10 INTERNALDATE "20-Nov-2026 00:00:00 +0000" RFC822.SIZE 100)',
                b'2 (UID 11 INTERNALDATE "22-Nov-2026 12:00:00 +0000" RFC822.SIZE 2048)',
            ],
        ),
    ]

    client = YahooImapClient(
        "user@yahoo.com",
        "app-pw",
        imap_factory=lambda _h, _p: fake_conn,
    )
    cutoff = datetime(2026, 11, 21, 0, 0, 0, tzinfo=UTC)

    with client:
        candidates, total, skipped_old = client.list_recent_candidates(cutoff)

    assert total == 2
    assert skipped_old == 1
    assert len(candidates) == 1
    assert candidates[0].uid == "11"
    assert candidates[0].size_bytes == 2048


def test_fetch_raw_message_uses_body_peek() -> None:
    fake_conn = MagicMock()
    fake_conn.login.return_value = ("OK", [b"ok"])
    fake_conn.select.return_value = ("OK", [b"1"])
    fake_conn.uid.return_value = (
        "OK",
        [(b"1 (UID 42 BODY[] {12}", b"Subject: Hi\r\n"), b")"],
    )

    client = YahooImapClient("u@yahoo.com", "pw", imap_factory=lambda _h, _p: fake_conn)
    with client:
        raw = client.fetch_raw_message("42")

    assert raw == b"Subject: Hi\r\n"
    fake_conn.uid.assert_called_with("FETCH", "42", "(BODY.PEEK[])")


def test_mark_read_and_move_prefers_imap_move_when_capable() -> None:
    fake_conn = MagicMock()
    fake_conn.login.return_value = ("OK", [b"ok"])
    fake_conn.select.return_value = ("OK", [b"1"])
    fake_conn.capabilities = ("IMAP4REV1", "MOVE")
    fake_conn.uid.return_value = ("OK", [b"ok"])

    client = YahooImapClient("u@yahoo.com", "pw", imap_factory=lambda _h, _p: fake_conn)
    with client:
        client.mark_read_and_move("99")

    assert fake_conn.uid.call_args_list[0].args == ("STORE", "99", "+FLAGS.SILENT", "(\\Seen)")
    assert fake_conn.uid.call_args_list[1].args == ("MOVE", "99", '"Forwarded"')


def test_mark_read_and_move_falls_back_to_copy_delete_expunge() -> None:
    fake_conn = MagicMock()
    fake_conn.login.return_value = ("OK", [b"ok"])
    fake_conn.select.return_value = ("OK", [b"1"])
    fake_conn.capabilities = ("IMAP4REV1",)
    fake_conn.uid.return_value = ("OK", [b"ok"])
    fake_conn.expunge.return_value = ("OK", [b"1"])

    client = YahooImapClient("u@yahoo.com", "pw", imap_factory=lambda _h, _p: fake_conn)
    with client:
        client.mark_read_and_move("99")

    assert fake_conn.uid.call_args_list[0].args == ("STORE", "99", "+FLAGS.SILENT", "(\\Seen)")
    assert fake_conn.uid.call_args_list[1].args == ("COPY", "99", '"Forwarded"')
    assert fake_conn.uid.call_args_list[2].args == ("STORE", "99", "+FLAGS.SILENT", "(\\Deleted)")
    fake_conn.expunge.assert_called_once()


def test_transient_connection_failure_retries_and_recovers() -> None:
    bad_conn = MagicMock()
    bad_conn.login.side_effect = TimeoutError("socket timed out")

    good_conn = MagicMock()
    good_conn.login.return_value = ("OK", [b"ok"])
    good_conn.select.return_value = ("OK", [b"0"])
    good_conn.uid.return_value = ("OK", [b""])

    conns = iter([bad_conn, good_conn])
    client = YahooImapClient(
        "u@yahoo.com",
        "pw",
        imap_factory=lambda _h, _p: next(conns),
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(YahooImapError):
        client.connect()

    # Reconnect using the second connection works cleanly.
    cutoff = datetime.now(UTC) - timedelta(hours=48)
    with client:
        candidates, total, _ = client.list_recent_candidates(cutoff)
    assert candidates == []
    assert total == 0
