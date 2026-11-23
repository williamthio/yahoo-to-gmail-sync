"""Unit tests for the Yahoo IMAP client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from yahoo_to_gmail_sync.yahoo_client import (
    YahooImapClient,
    format_imap_date,
    parse_imap_internaldate,
)


def _mock_conn(*, post_auth_caps: bytes = b"IMAP4REV1 MOVE UIDPLUS") -> MagicMock:
    conn = MagicMock()
    conn.login.return_value = ("OK", [b"Logged in"])
    conn.capability.return_value = ("OK", [post_auth_caps])
    conn.select.return_value = ("OK", [b"1"])
    return conn


def test_parse_and_format_imap_dates_are_locale_independent() -> None:
    dt = parse_imap_internaldate(" 2-Nov-2026 10:00:00 +0700")
    assert dt == datetime(2026, 11, 2, 3, 0, 0, tzinfo=UTC)
    assert format_imap_date(dt) == "02-Nov-2026"

    with pytest.raises(ValueError, match="INTERNALDATE"):
        parse_imap_internaldate("not-a-date")


def test_list_recent_candidates_handles_multiline_search_and_cutoff() -> None:
    fake_conn = _mock_conn()
    fake_conn.uid.side_effect = [
        ("OK", [b"10", b"11"]),
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
    fake_conn = _mock_conn()
    fake_conn.uid.return_value = (
        "OK",
        [(b"1 (UID 42 BODY[] {12}", b"Subject: Hi\r\n"), b")"],
    )

    client = YahooImapClient("u@yahoo.com", "pw", imap_factory=lambda _h, _p: fake_conn)
    with client:
        raw = client.fetch_raw_message("42")

    assert raw == b"Subject: Hi\r\n"
    fake_conn.uid.assert_called_with("FETCH", "42", "(BODY.PEEK[])")


def test_mark_read_and_move_uses_post_auth_capabilities() -> None:
    # Pre-auth capabilities omit MOVE, but post-auth CAPABILITY advertises MOVE.
    fake_conn = _mock_conn(post_auth_caps=b"IMAP4REV1 MOVE UIDPLUS")
    fake_conn.capabilities = ("IMAP4REV1",)
    fake_conn.uid.return_value = ("OK", [b"ok"])

    client = YahooImapClient("u@yahoo.com", "pw", imap_factory=lambda _h, _p: fake_conn)
    with client:
        client.mark_read_and_move("99")

    fake_conn.capability.assert_called_once()
    assert fake_conn.uid.call_args_list[0].args == ("STORE", "99", "+FLAGS.SILENT", "(\\Seen)")
    assert fake_conn.uid.call_args_list[1].args == ("MOVE", "99", '"Forwarded"')


def test_mark_read_and_move_fallback_avoids_duplicate_copy_on_retry() -> None:
    conn1 = _mock_conn(post_auth_caps=b"IMAP4REV1 UIDPLUS")
    conn1.uid.side_effect = [
        ("OK", [b"ok"]),  # STORE \Seen
        ("OK", [b"ok"]),  # COPY succeeds
        TimeoutError("drop before STORE \\Deleted"),
    ]

    conn2 = _mock_conn(post_auth_caps=b"IMAP4REV1 UIDPLUS")
    conn2.uid.return_value = ("OK", [b"ok"])

    conns = iter([conn1, conn2])
    client = YahooImapClient(
        "u@yahoo.com",
        "pw",
        imap_factory=lambda _h, _p: next(conns),
        sleep_fn=lambda _s: None,
    )
    with client:
        client.mark_read_and_move("99")

    # On retry, COPY must NOT be called a second time.
    retry_commands = [call.args[0] for call in conn2.uid.call_args_list]
    assert "COPY" not in retry_commands
    assert retry_commands == ["STORE", "STORE", "EXPUNGE"]


def test_ensure_archive_folder_creates_when_missing() -> None:
    fake_conn = _mock_conn()
    fake_conn.list.return_value = ("OK", [b""])
    fake_conn.create.return_value = ("OK", [b"Created"])

    client = YahooImapClient("u@yahoo.com", "pw", imap_factory=lambda _h, _p: fake_conn)
    with client:
        client.ensure_archive_folder()

    fake_conn.create.assert_called_once_with('"Forwarded"')


def test_transient_connect_and_reconnect_failures_retry_automatically() -> None:
    bad_conn_1 = MagicMock()
    bad_conn_1.login.side_effect = TimeoutError("socket timed out")

    bad_conn_2 = MagicMock()
    bad_conn_2.login.return_value = ("OK", [b"ok"])
    bad_conn_2.capability.return_value = ("OK", [b"IMAP4REV1"])
    bad_conn_2.select.side_effect = TimeoutError("select timed out")

    good_conn = _mock_conn()
    good_conn.uid.return_value = ("OK", [b""])

    conns = iter([bad_conn_1, bad_conn_2, good_conn])
    client = YahooImapClient(
        "u@yahoo.com",
        "pw",
        imap_factory=lambda _h, _p: next(conns),
        sleep_fn=lambda _s: None,
    )

    cutoff = datetime.now(UTC) - timedelta(hours=48)
    with client:
        candidates, total, _ = client.list_recent_candidates(cutoff)

    assert candidates == []
    assert total == 0
    # Failed sockets were cleaned up before retrying.
    bad_conn_1.logout.assert_called_once()
    bad_conn_2.logout.assert_called_once()
