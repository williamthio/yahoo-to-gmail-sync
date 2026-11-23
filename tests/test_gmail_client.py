"""Unit tests for the Gmail API client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from yahoo_to_gmail_sync.gmail_client import (
    MAX_IMPORT_BYTES,
    GmailApiClient,
    GmailApiError,
    extract_message_id,
)


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp=resp, content=b'{"error":"sensitive_payload_should_not_leak"}')


def test_extract_message_id_normalizes_folded_header() -> None:
    raw = b"From: a@example.com\r\nMessage-ID:\r\n  <abc123@mail.yahoo.com>\r\n\r\nBody"
    assert extract_message_id(raw) == "<abc123@mail.yahoo.com>"


def test_extract_message_id_missing_returns_none() -> None:
    raw = b"From: a@example.com\r\nSubject: No ID\r\n\r\nBody"
    assert extract_message_id(raw) is None


def test_ensure_label_reuses_existing_or_creates() -> None:
    service = MagicMock()
    labels_api = service.users.return_value.labels.return_value
    labels_api.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Yahoo"}]
    }

    client = GmailApiClient("cid", "cs", "rt", service=service)
    assert client.ensure_label("Yahoo") == "Label_1"
    labels_api.create.assert_not_called()

    labels_api.list.return_value.execute.return_value = {"labels": []}
    labels_api.create.return_value.execute.return_value = {"id": "Label_2", "name": "Yahoo"}
    assert client.ensure_label("Yahoo") == "Label_2"
    labels_api.create.assert_called_once()


def test_message_exists_uses_rfc822msgid_and_includes_spam_trash() -> None:
    service = MagicMock()
    msgs_api = service.users.return_value.messages.return_value
    msgs_api.list.return_value.execute.return_value = {"messages": [{"id": "gmail-1"}]}

    client = GmailApiClient("cid", "cs", "rt", service=service)
    assert client.message_exists("<abc@mail.yahoo.com>") is True
    msgs_api.list.assert_called_once_with(
        userId="me",
        q="rfc822msgid:<abc@mail.yahoo.com>",
        includeSpamTrash=True,
        maxResults=1,
    )


def test_import_retries_on_429_and_sanitizes_errors() -> None:
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
