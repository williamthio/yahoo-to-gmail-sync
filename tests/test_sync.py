"""Unit tests for the end-to-end sync orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime

from yahoo_to_gmail_sync.config import SyncConfig
from yahoo_to_gmail_sync.gmail_client import MAX_IMPORT_BYTES, GmailApiError
from yahoo_to_gmail_sync.sync import run_sync
from yahoo_to_gmail_sync.yahoo_client import YahooCandidate, YahooImapError


class FakeYahooClient:
    def __init__(
        self,
        candidates: list[YahooCandidate],
        payloads: dict[str, bytes],
        *,
        fail_move_uids: set[str] | None = None,
    ) -> None:
        self._candidates = candidates
        self._payloads = payloads
        self._fail_move_uids = fail_move_uids or set()
        self.moved: list[str] = []
        self.archive_folder_ensured = False

    def __enter__(self) -> FakeYahooClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ensure_archive_folder(self) -> None:
        self.archive_folder_ensured = True

    def list_recent_candidates(self, cutoff_utc: datetime) -> tuple[list[YahooCandidate], int, int]:
        eligible = [c for c in self._candidates if c.internal_date >= cutoff_utc]
        skipped_old = len(self._candidates) - len(eligible)
        return eligible, len(self._candidates), skipped_old

    def fetch_raw_message(self, uid: str) -> bytes:
        return self._payloads[uid]

    def mark_read_and_move(self, uid: str) -> None:
        if uid in self._fail_move_uids:
            raise YahooImapError(f"Yahoo IMAP MOVE failed for UID {uid}", retryable=True)
        self.moved.append(uid)


class FakeGmailClient:
    def __init__(self, existing_ids: set[str] | None = None, fail_ids: set[str] | None = None):
        self.existing_ids = existing_ids or set()
        self.fail_ids = fail_ids or set()
        self.checked_ids: list[str] = []
        self.imported: list[tuple[bytes, list[str]]] = []

    def ensure_label(self, label_name: str) -> str:
        assert label_name == "Yahoo"
        return "Label_Yahoo"

    def message_exists(self, message_id: str) -> bool:
        self.checked_ids.append(message_id)
        return message_id in self.existing_ids

    def import_message(self, raw_bytes: bytes, label_ids: list[str]) -> str:
        if any(marker.encode() in raw_bytes for marker in self.fail_ids):
            raise GmailApiError("Gmail API messages.import failed with HTTP 400", status_code=400)
        self.imported.append((raw_bytes, label_ids))
        return "gmail-new-id"


def _config() -> SyncConfig:
    return SyncConfig(
        yahoo_email="u@yahoo.com",
        yahoo_app_password="pw",
        gmail_client_id="cid",
        gmail_client_secret="cs",
        gmail_refresh_token="rt",
    )


def test_run_sync_imports_new_dedups_existing_and_isolates_failures() -> None:
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    candidates = [
        # Too old (> 48h)
        YahooCandidate("1", datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC), 100),
        # Duplicate already in Gmail
        YahooCandidate("2", datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC), 100),
        # Fails import
        YahooCandidate("3", datetime(2026, 11, 22, 10, 30, 0, tzinfo=UTC), 100),
        # Succeeds import
        YahooCandidate("4", datetime(2026, 11, 22, 11, 0, 0, tzinfo=UTC), 100),
        # Oversized (> 35 MB)
        YahooCandidate(
            "5",
            datetime(2026, 11, 22, 11, 30, 0, tzinfo=UTC),
            MAX_IMPORT_BYTES + 10,
        ),
    ]
    payloads = {
        "2": b"Message-ID: <dup@yahoo.com>\r\n\r\nDuplicate",
        "3": b"Message-ID: <bad@yahoo.com>\r\n\r\nBAD_PAYLOAD",
        "4": b"Message-ID: <new@yahoo.com>\r\n\r\nHello",
    }

    yahoo = FakeYahooClient(candidates, payloads)
    gmail = FakeGmailClient(existing_ids={"<dup@yahoo.com>"}, fail_ids={"BAD_PAYLOAD"})

    stats = run_sync(_config(), yahoo_client=yahoo, gmail_client=gmail, now_utc=now)

    assert stats.scanned == 5
    assert stats.skipped_too_old == 1
    assert stats.eligible == 4
    assert stats.skipped_duplicate == 1
    assert stats.imported == 1
    assert stats.failed == 2
    assert stats.failed_uids == ["3", "5"]

    # UID 2 (duplicate) and UID 4 (imported) were moved; UID 3 & 5 remain in INBOX.
    assert yahoo.moved == ["2", "4"]
    assert yahoo.archive_folder_ensured is True
    assert len(gmail.imported) == 1
    assert gmail.imported[0][1] == ["INBOX", "UNREAD", "Label_Yahoo"]


def test_run_sync_empty_candidates_skips_archive_folder_check() -> None:
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    yahoo = FakeYahooClient([], {})
    gmail = FakeGmailClient()

    stats = run_sync(_config(), yahoo_client=yahoo, gmail_client=gmail, now_utc=now)

    assert stats.scanned == 0
    assert stats.eligible == 0
    assert yahoo.archive_folder_ensured is False


def test_run_sync_handles_missing_message_id_and_post_import_move_failure() -> None:
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    candidates = [
        YahooCandidate("10", datetime(2026, 11, 22, 11, 0, 0, tzinfo=UTC), 100),
        YahooCandidate("11", datetime(2026, 11, 22, 11, 5, 0, tzinfo=UTC), 100),
    ]
    payloads = {
        "10": b"Subject: No Message-ID header\r\n\r\nBody",
        "11": b"Message-ID: <move-fail@yahoo.com>\r\n\r\nBody",
    }

    yahoo = FakeYahooClient(candidates, payloads, fail_move_uids={"11"})
    gmail = FakeGmailClient()

    stats = run_sync(_config(), yahoo_client=yahoo, gmail_client=gmail, now_utc=now)

    # UID 10 had no Message-ID so message_exists was skipped; UID 11 imported then move failed.
    assert gmail.checked_ids == ["<move-fail@yahoo.com>"]
    assert len(gmail.imported) == 2
    assert yahoo.moved == ["10"]
    assert stats.imported == 1
    assert stats.failed == 1
    assert stats.failed_uids == ["11"]


def test_run_sync_stops_gracefully_on_shutdown_signal() -> None:
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    candidates = [
        YahooCandidate("20", datetime(2026, 11, 22, 11, 0, 0, tzinfo=UTC), 100),
        YahooCandidate("21", datetime(2026, 11, 22, 11, 5, 0, tzinfo=UTC), 100),
    ]
    payloads = {
        "20": b"Message-ID: <one@yahoo.com>\r\n\r\nOne",
        "21": b"Message-ID: <two@yahoo.com>\r\n\r\nTwo",
    }
    yahoo = FakeYahooClient(candidates, payloads)
    gmail = FakeGmailClient()

    checks = iter([False, True])
    stats = run_sync(
        _config(),
        yahoo_client=yahoo,
        gmail_client=gmail,
        now_utc=now,
        should_stop=lambda: next(checks),
    )

    assert stats.imported == 1
    assert stats.deferred == 1
    assert stats.failed == 0
    assert yahoo.moved == ["20"]
