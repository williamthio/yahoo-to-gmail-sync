"""Unit tests for the Gmail API client."""

from __future__ import annotations

from unittest.mock import MagicMock

import httplib2
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from yahoo_to_gmail_sync.gmail_client import (
    MAX_IMPORT_BYTES,
    RESUMABLE_CHUNK_BYTES,
    RESUMABLE_THRESHOLD_BYTES,
    GmailApiClient,
    GmailApiError,
    extract_message_id,
)


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp=resp, content=b'{"error":"sensitive_payload_should_not_leak"}')


def test_extract_message_id_normalizes_folded_header_and_strips_comments() -> None:
    raw = b"From: a@example.com\r\nMessage-ID:\r\n  <abc123@mail.yahoo.com> (comment)\r\n\r\nBody"
    assert extract_message_id(raw) == "<abc123@mail.yahoo.com>"


def test_extract_message_id_handles_lf_only_separator_and_missing_header() -> None:
    raw_lf = b"From: a@example.com\nMessage-ID: <lf@example.com>\n\nBody"
    assert extract_message_id(raw_lf) == "<lf@example.com>"

    raw_missing = b"From: a@example.com\r\nSubject: No ID\r\n\r\nBody"
    assert extract_message_id(raw_missing) is None


def test_ensure_label_reuses_existing_case_insensitively_or_creates() -> None:
    service = MagicMock()
    labels_api = service.users.return_value.labels.return_value
    labels_api.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "yahoo"}]
    }

    client = GmailApiClient("cid", "cs", "rt", service=service)
    assert client.ensure_label("Yahoo") == "Label_1"
    labels_api.create.assert_not_called()

    labels_api.list.return_value.execute.return_value = {"labels": []}
    labels_api.create.return_value.execute.return_value = {"id": "Label_2", "name": "Yahoo"}
    assert client.ensure_label("Yahoo") == "Label_2"
    labels_api.create.assert_called_once()


def test_ensure_label_recovers_from_409_conflict_race() -> None:
    service = MagicMock()
    labels_api = service.users.return_value.labels.return_value
    labels_api.list.return_value.execute.side_effect = [
        {"labels": []},
        {"labels": [{"id": "Label_Raced", "name": "Yahoo"}]},
    ]
    labels_api.create.return_value.execute.side_effect = _http_error(409)

    client = GmailApiClient("cid", "cs", "rt", service=service, sleep_fn=lambda _s: None)
    assert client.ensure_label("Yahoo") == "Label_Raced"
    assert labels_api.list.call_count == 2


def test_discovery_build_retries_transient_transport_errors() -> None:
    service = MagicMock()
    labels_api = service.users.return_value.labels.return_value
    labels_api.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Yahoo"}]
    }

    attempts = 0

    def _flaky_factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httplib2.ServerNotFoundError("transient discovery DNS failure")
        return service

    client = GmailApiClient("cid", "cs", "rt", service_factory=_flaky_factory, sleep_fn=lambda _: 0)
    assert client.ensure_label("Yahoo") == "Label_1"
    assert attempts == 2


def test_message_exists_escapes_quotes_and_trailing_backslash() -> None:
    service = MagicMock()
    msgs_api = service.users.return_value.messages.return_value
    msgs_api.list.return_value.execute.return_value = {"messages": [{"id": "gmail-1"}]}

    client = GmailApiClient("cid", "cs", "rt", service=service)
    assert client.message_exists('foo" OR label:inbox\\') is True
    msgs_api.list.assert_called_once_with(
        userId="me",
        q='rfc822msgid:"foo\\" OR label:inbox\\\\"',
        includeSpamTrash=True,
        maxResults=1,
    )


def test_idempotent_reads_retry_on_transport_and_5xx_errors() -> None:
    service = MagicMock()
    msgs_api = service.users.return_value.messages.return_value
    msgs_api.list.return_value.execute.side_effect = [
        httplib2.ServerNotFoundError("dns blip"),
        TransportError("oauth refresh hiccup"),
        _http_error(503),
        {"messages": []},
    ]

    client = GmailApiClient("cid", "cs", "rt", service=service, sleep_fn=lambda _s: None)
    assert client.message_exists("<abc@mail.yahoo.com>") is False
    assert msgs_api.list.call_count == 4


def test_import_retries_on_429_but_does_not_blindly_retry_ambiguous_503() -> None:
    service = MagicMock()
    msgs_api = service.users.return_value.messages.return_value
    msgs_api.import_.return_value.execute.side_effect = [
        _http_error(429),
        {"id": "imported-1"},
    ]

    client = GmailApiClient("cid", "cs", "rt", service=service, sleep_fn=lambda _s: None)
    msg_id = client.import_message(b"Subject: Test\r\n\r\nHi", ["INBOX", "UNREAD", "Label_1"])
    assert msg_id == "imported-1"
    assert msgs_api.import_.call_count == 2

    msgs_api.import_.reset_mock()
    msgs_api.import_.return_value.execute.side_effect = _http_error(503)
    with pytest.raises(GmailApiError, match="HTTP 503") as exc_info:
        client.import_message(b"Subject: Test\r\n\r\nHi", ["INBOX"])
    assert exc_info.value.retryable is True
    assert msgs_api.import_.call_count == 1


def test_large_message_uses_resumable_upload_with_explicit_chunksize() -> None:
    service = MagicMock()
    msgs_api = service.users.return_value.messages.return_value
    msgs_api.import_.return_value.execute.return_value = {"id": "imported-large"}

    client = GmailApiClient("cid", "cs", "rt", service=service)
    payload = b"Subject: Big\r\n\r\n" + (b"a" * (RESUMABLE_THRESHOLD_BYTES + 1))
    assert client.import_message(payload, ["INBOX"]) == "imported-large"

    media = msgs_api.import_.call_args.kwargs["media_body"]
    assert media.resumable() is True
    assert media.chunksize() == RESUMABLE_CHUNK_BYTES


def test_non_retryable_http_error_does_not_leak_body() -> None:
    service = MagicMock()
    msgs_api = service.users.return_value.messages.return_value
    msgs_api.import_.return_value.execute.side_effect = _http_error(400)

    client = GmailApiClient("cid", "cs", "rt", service=service, sleep_fn=lambda _s: None)
    with pytest.raises(GmailApiError) as exc_info:
        client.import_message(b"Subject: Bad\r\n\r\nHi", ["INBOX"])

    assert "sensitive_payload_should_not_leak" not in str(exc_info.value)
    assert "HTTP 400" in str(exc_info.value)


def test_oversized_message_rejected_before_upload() -> None:
    service = MagicMock()
    client = GmailApiClient("cid", "cs", "rt", service=service)
    with pytest.raises(GmailApiError, match="35 MB"):
        client.import_message(b"x" * (MAX_IMPORT_BYTES + 1), ["INBOX"])
