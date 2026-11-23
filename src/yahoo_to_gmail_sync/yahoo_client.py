"""Yahoo Mail IMAP client with zero-PII error handling."""

from __future__ import annotations

import imaplib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_META_PATTERN = re.compile(
    rb"UID\s+(?P<uid>\d+).*?"
    rb'INTERNALDATE\s+"(?P<date>[^"]+)".*?'
    rb"RFC822\.SIZE\s+(?P<size>\d+)",
    re.IGNORECASE | re.DOTALL,
)
_UID_PATTERN = re.compile(rb"UID\s+(\d+)", re.IGNORECASE)
_INTERNALDATE_PATTERN = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
_SIZE_PATTERN = re.compile(rb"RFC822\.SIZE\s+(\d+)", re.IGNORECASE)

_IMAP_TIMEOUT_SECONDS = 30
_FETCH_META_BATCH_SIZE = 100
_MAX_RETRIES = 3


class YahooImapError(RuntimeError):
    """Sanitized IMAP error that never embeds email headers, bodies, or credentials."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class YahooCandidate:
    """Metadata for a candidate email in Yahoo INBOX."""

    uid: str
    internal_date: datetime
    size_bytes: int


def parse_imap_internaldate(value: str) -> datetime:
    """Parse an IMAP INTERNALDATE string (e.g. '22-Nov-2026 08:15:30 +0000') to UTC."""
    normalized = " ".join(value.strip().split())
    parsed = datetime.strptime(normalized, "%d-%b-%Y %H:%M:%S %z")
    return parsed.astimezone(UTC)


def _quote_mailbox(name: str) -> str:
    """Quote an IMAP mailbox name safely when it contains spaces or special characters."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_metadata_item(item: bytes) -> YahooCandidate | None:
    """Extract UID, INTERNALDATE, and RFC822.SIZE from a single IMAP FETCH metadata line."""
    match = _META_PATTERN.search(item)
    if match:
        uid = match.group("uid").decode("ascii")
        date_str = match.group("date").decode("ascii")
        size = int(match.group("size").decode("ascii"))
        return YahooCandidate(
            uid=uid,
            internal_date=parse_imap_internaldate(date_str),
            size_bytes=size,
        )

    # Fallback when server returns attributes in a different order than requested.
    uid_m = _UID_PATTERN.search(item)
    date_m = _INTERNALDATE_PATTERN.search(item)
    size_m = _SIZE_PATTERN.search(item)
    if not (uid_m and date_m and size_m):
        return None
    return YahooCandidate(
        uid=uid_m.group(1).decode("ascii"),
        internal_date=parse_imap_internaldate(date_m.group(1).decode("ascii")),
        size_bytes=int(size_m.group(1).decode("ascii")),
    )


def _extract_raw_bytes(fetch_data: list[object]) -> bytes | None:
    """Extract raw RFC 822 message bytes from an IMAP FETCH response."""
    for entry in fetch_data:
        if (
            isinstance(entry, tuple)
            and len(entry) >= 2
            and isinstance(entry[1], (bytes, bytearray))
        ):
            return bytes(entry[1])
    return None


class YahooImapClient:
    """Manages an authenticated TLS IMAP session against Yahoo Mail."""

    def __init__(
        self,
        email_address: str,
        app_password: str,
        *,
        host: str = "imap.mail.yahoo.com",
        port: int = 993,
        source_folder: str = "INBOX",
        archive_folder: str = "Forwarded",
        imap_factory: Callable[[str, int], imaplib.IMAP4_SSL] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._email_address = email_address
        self._app_password = app_password
        self._host = host
        self._port = port
        self._source_folder = source_folder
        self._archive_folder = archive_folder
        self._imap_factory = imap_factory or (
            lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=_IMAP_TIMEOUT_SECONDS)
        )
        self._sleep = sleep_fn
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> YahooImapClient:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open a TLS IMAP connection, authenticate, and select the source folder."""
        self.close()
        try:
            conn = self._imap_factory(self._host, self._port)
            # Ensure wire-level IMAP debugging never leaks credentials or email payloads.
            conn.debug = 0
            status, _ = conn.login(self._email_address, self._app_password)
            if status != "OK":
                raise YahooImapError("Yahoo IMAP login rejected", retryable=False)

            sel_status, _ = conn.select(_quote_mailbox(self._source_folder), readonly=False)
            if sel_status != "OK":
                raise YahooImapError("Failed to select Yahoo source folder", retryable=False)

            self._conn = conn
        except YahooImapError:
            raise
        except (OSError, TimeoutError, imaplib.IMAP4.abort) as exc:
            raise YahooImapError(
                f"Yahoo IMAP transient connection error ({type(exc).__name__})",
                retryable=True,
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise YahooImapError(
                f"Yahoo IMAP protocol error ({type(exc).__name__})",
                retryable=False,
            ) from exc

    def close(self) -> None:
        """Close the IMAP session cleanly."""
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except Exception:
            try:
                self._conn.shutdown()
            except Exception as exc:
                logger.debug("Ignored error during IMAP shutdown: %s", type(exc).__name__)
        finally:
            self._conn = None

    def _require_conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _with_retry(self, operation_name: str, fn: Callable[[imaplib.IMAP4_SSL], object]) -> object:
        last_exc: YahooImapError | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                conn = self._require_conn()
                return fn(conn)
            except YahooImapError as exc:
                last_exc = exc
                if not exc.retryable or attempt == _MAX_RETRIES:
                    raise
            except (OSError, TimeoutError, imaplib.IMAP4.abort) as exc:
                last_exc = YahooImapError(
                    f"Yahoo IMAP transient error during {operation_name} ({type(exc).__name__})",
                    retryable=True,
                )
                if attempt == _MAX_RETRIES:
                    raise last_exc from exc
            except imaplib.IMAP4.error as exc:
                raise YahooImapError(
                    f"Yahoo IMAP error during {operation_name} ({type(exc).__name__})",
                    retryable=False,
                ) from exc

            delay = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)  # noqa: S311
            logger.warning(
                "Retrying Yahoo IMAP %s (attempt %d/%d) after %.2fs",
                operation_name,
                attempt,
                _MAX_RETRIES,
                delay,
            )
            self._sleep(delay)
            self.connect()

        assert last_exc is not None
        raise last_exc

    def ensure_archive_folder(self) -> None:
        """Create the destination folder on Yahoo if it does not already exist."""

        def _op(conn: imaplib.IMAP4_SSL) -> None:
            status, list_data = conn.list('""', _quote_mailbox(self._archive_folder))
            if status == "OK" and list_data and any(item is not None for item in list_data):
                return

            create_status, _ = conn.create(_quote_mailbox(self._archive_folder))
            if create_status != "OK":
                # Re-check in case the server reported ALREADYEXISTS or similar.
                recheck_status, recheck_data = conn.list('""', _quote_mailbox(self._archive_folder))
                if recheck_status == "OK" and recheck_data and any(recheck_data):
                    return
                raise YahooImapError("Failed to create Yahoo archive folder", retryable=False)

        self._with_retry("ensure_archive_folder", _op)

    def list_recent_candidates(self, cutoff_utc: datetime) -> tuple[list[YahooCandidate], int, int]:
        """Return (eligible_candidates, total_scanned, skipped_too_old) from INBOX.

        Uses IMAP SINCE (date-only, with a 1-day safety buffer) followed by an exact
        UTC INTERNALDATE check so timezone differences never drop recent mail.
        """
        search_since = (cutoff_utc - timedelta(days=1)).strftime("%d-%b-%Y")

        def _search_and_fetch_meta(
            conn: imaplib.IMAP4_SSL,
        ) -> tuple[list[YahooCandidate], int, int]:
            status, search_data = conn.uid("SEARCH", None, "SINCE", search_since)
            if status != "OK":
                raise YahooImapError("Yahoo IMAP UID SEARCH failed", retryable=True)

            if not search_data or not search_data[0]:
                return [], 0, 0

            raw_uids = search_data[0]
            uid_tokens = (
                raw_uids.decode("ascii").split()
                if isinstance(raw_uids, (bytes, bytearray))
                else str(raw_uids).split()
            )
            if not uid_tokens:
                return [], 0, 0

            eligible: list[YahooCandidate] = []
            skipped_too_old = 0

            for start in range(0, len(uid_tokens), _FETCH_META_BATCH_SIZE):
                batch = uid_tokens[start : start + _FETCH_META_BATCH_SIZE]
                uid_set = ",".join(batch)
                f_status, fetch_data = conn.uid("FETCH", uid_set, "(INTERNALDATE RFC822.SIZE)")
                if f_status != "OK":
                    raise YahooImapError("Yahoo IMAP metadata FETCH failed", retryable=True)

                for entry in fetch_data or []:
                    raw_line = entry[0] if isinstance(entry, tuple) else entry
                    if not isinstance(raw_line, (bytes, bytearray)):
                        continue
                    candidate = _parse_metadata_item(bytes(raw_line))
                    if candidate is None:
                        continue
                    if candidate.internal_date < cutoff_utc:
                        skipped_too_old += 1
                    else:
                        eligible.append(candidate)

            eligible.sort(key=lambda item: item.internal_date)
            return eligible, len(uid_tokens), skipped_too_old

        result = self._with_retry("list_recent_candidates", _search_and_fetch_meta)
        return result  # type: ignore[return-value]

    def fetch_raw_message(self, uid: str) -> bytes:
        """Fetch raw RFC 822 message bytes using BODY.PEEK[] without setting \\Seen."""

        def _fetch(conn: imaplib.IMAP4_SSL) -> bytes:
            status, fetch_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not fetch_data:
                raise YahooImapError(f"Yahoo IMAP BODY.PEEK[] failed for UID {uid}", retryable=True)
            raw_bytes = _extract_raw_bytes(list(fetch_data))
            if raw_bytes is None:
                raise YahooImapError(f"Empty payload returned for UID {uid}", retryable=False)
            return raw_bytes

        result = self._with_retry(f"fetch_raw_message({uid})", _fetch)
        return result  # type: ignore[return-value]

    def mark_read_and_move(self, uid: str) -> None:
        """Mark a message as READ (\\Seen) and move it out of INBOX to the archive folder."""
        target = _quote_mailbox(self._archive_folder)

        def _move(conn: imaplib.IMAP4_SSL) -> None:
            store_status, _ = conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
            if store_status != "OK":
                raise YahooImapError(f"Failed to mark UID {uid} as read", retryable=True)

            capabilities = {cap.upper() for cap in getattr(conn, "capabilities", ())}
            if "MOVE" in capabilities or b"MOVE" in capabilities:
                move_status, _ = conn.uid("MOVE", uid, target)
                if move_status != "OK":
                    raise YahooImapError(f"Yahoo IMAP MOVE failed for UID {uid}", retryable=True)
                return

            copy_status, _ = conn.uid("COPY", uid, target)
            if copy_status != "OK":
                raise YahooImapError(f"Yahoo IMAP COPY failed for UID {uid}", retryable=True)

            del_status, _ = conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
            if del_status != "OK":
                raise YahooImapError(f"Failed to mark UID {uid} deleted after COPY", retryable=True)

            if "UIDPLUS" in capabilities or b"UIDPLUS" in capabilities:
                exp_status, _ = conn.uid("EXPUNGE", uid)
            else:
                exp_status, _ = conn.expunge()
            if exp_status != "OK":
                raise YahooImapError(f"Yahoo IMAP EXPUNGE failed for UID {uid}", retryable=True)

        self._with_retry(f"mark_read_and_move({uid})", _move)
