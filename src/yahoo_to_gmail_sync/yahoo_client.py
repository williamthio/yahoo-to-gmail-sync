"""Yahoo Mail IMAP client with zero-PII error handling."""

from __future__ import annotations

import imaplib
import logging
import random
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_IMAP_MONTH_NAMES = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_IMAP_MONTH_NUMBERS = {name.lower(): idx for idx, name in enumerate(_IMAP_MONTH_NAMES) if name}
_INTERNALDATE_VALUE_PATTERN = re.compile(
    r"^(?P<day>\d{1,2})-(?P<month>[A-Za-z]{3})-(?P<year>\d{4})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
    r"(?P<tz_sign>[+-])(?P<tz_hour>\d{2})(?P<tz_min>\d{2})$"
)

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
    """Parse an RFC 3501 IMAP INTERNALDATE string to UTC without locale dependence."""
    normalized = " ".join(value.strip().split())
    match = _INTERNALDATE_VALUE_PATTERN.match(normalized)
    if not match:
        raise ValueError("Invalid IMAP INTERNALDATE format")

    month = _IMAP_MONTH_NUMBERS.get(match.group("month").lower())
    if month is None:
        raise ValueError("Unknown IMAP month abbreviation")

    offset_minutes = int(match.group("tz_hour")) * 60 + int(match.group("tz_min"))
    if match.group("tz_sign") == "-":
        offset_minutes = -offset_minutes

    parsed = datetime(
        year=int(match.group("year")),
        month=month,
        day=int(match.group("day")),
        hour=int(match.group("hour")),
        minute=int(match.group("minute")),
        second=int(match.group("second")),
        tzinfo=timezone(timedelta(minutes=offset_minutes)),
    )
    return parsed.astimezone(UTC)


def format_imap_date(dt: datetime) -> str:
    """Format a date in RFC 3501 IMAP format (DD-Mon-YYYY) without locale dependence."""
    return f"{dt.day:02d}-{_IMAP_MONTH_NAMES[dt.month]}-{dt.year}"


def _quote_mailbox(name: str) -> str:
    """Quote an IMAP mailbox name safely when it contains spaces or special characters."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _default_imap_factory(host: str, port: int) -> imaplib.IMAP4_SSL:
    """Create a TLS-verified IMAP4_SSL connection (imaplib defaults to CERT_NONE)."""
    return imaplib.IMAP4_SSL(
        host,
        port,
        ssl_context=ssl.create_default_context(),
        timeout=_IMAP_TIMEOUT_SECONDS,
    )


def _parse_metadata_item(item: bytes) -> YahooCandidate | None:
    """Extract UID, INTERNALDATE, and RFC822.SIZE from a single IMAP FETCH metadata line."""
    match = _META_PATTERN.search(item)
    if match:
        uid = match.group("uid").decode("ascii")
        date_str = match.group("date").decode("ascii")
        size = int(match.group("size").decode("ascii"))
    else:
        # Fallback when server returns attributes in a different order than requested.
        uid_m = _UID_PATTERN.search(item)
        date_m = _INTERNALDATE_PATTERN.search(item)
        size_m = _SIZE_PATTERN.search(item)
        if not (uid_m and date_m and size_m):
            return None
        uid = uid_m.group(1).decode("ascii")
        date_str = date_m.group(1).decode("ascii")
        size = int(size_m.group(1).decode("ascii"))

    try:
        internal_date = parse_imap_internaldate(date_str)
    except ValueError:
        logger.warning("Skipping UID %s due to unparseable IMAP INTERNALDATE", uid)
        return None

    return YahooCandidate(uid=uid, internal_date=internal_date, size_bytes=size)


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


def _close_connection(conn: imaplib.IMAP4_SSL | None, *, force: bool = False) -> None:
    """Best-effort close of an IMAP connection without raising.

    When force=True (error/reconnect paths), skip LOGOUT to avoid stalling another
    full socket timeout on a dead connection.
    """
    if conn is None:
        return
    if force:
        try:
            conn.shutdown()
        except Exception as exc:
            logger.debug("Ignored error during forced IMAP shutdown: %s", type(exc).__name__)
        return

    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception as exc:
            logger.debug("Ignored error during IMAP shutdown: %s", type(exc).__name__)


def _fetch_capabilities(conn: imaplib.IMAP4_SSL) -> frozenset[str]:
    """Query post-auth IMAP capabilities, falling back to pre-auth conn.capabilities."""
    tokens: set[str] = set()
    try:
        cap_status, cap_data = conn.capability()
    except Exception:
        cap_status, cap_data = None, None

    if cap_status == "OK" and cap_data:
        for entry in cap_data:
            if isinstance(entry, (bytes, bytearray)):
                tokens.update(entry.decode("ascii", errors="ignore").upper().split())
            elif isinstance(entry, str):
                tokens.update(entry.upper().split())

    if not tokens:
        for cap in getattr(conn, "capabilities", ()):
            if isinstance(cap, (bytes, bytearray)):
                tokens.add(cap.decode("ascii", errors="ignore").upper())
            elif isinstance(cap, str):
                tokens.add(cap.upper())

    return frozenset(tokens)


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
        self._imap_factory = imap_factory or _default_imap_factory
        self._sleep = sleep_fn
        self._conn: imaplib.IMAP4_SSL | None = None
        self._capabilities: frozenset[str] = frozenset()

    def __enter__(self) -> YahooImapClient:
        self._with_retry("connect", lambda _conn: None)
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open a TLS IMAP connection, authenticate, refresh capabilities, and select source."""
        self.close()
        conn: imaplib.IMAP4_SSL | None = None
        try:
            conn = self._imap_factory(self._host, self._port)
            # Ensure wire-level IMAP debugging never leaks credentials or email payloads.
            conn.debug = 0
            status, _ = conn.login(self._email_address, self._app_password)
            if status != "OK":
                raise YahooImapError("Yahoo IMAP login rejected", retryable=False)

            capabilities = _fetch_capabilities(conn)

            sel_status, _ = conn.select(_quote_mailbox(self._source_folder), readonly=False)
            if sel_status != "OK":
                raise YahooImapError("Failed to select Yahoo source folder", retryable=False)

            self._conn = conn
            self._capabilities = capabilities
        except YahooImapError:
            _close_connection(conn, force=True)
            raise
        except (OSError, TimeoutError, imaplib.IMAP4.abort) as exc:
            _close_connection(conn, force=True)
            raise YahooImapError(
                f"Yahoo IMAP transient connection error ({type(exc).__name__})",
                retryable=True,
            ) from exc
        except imaplib.IMAP4.error as exc:
            _close_connection(conn, force=True)
            raise YahooImapError(
                f"Yahoo IMAP protocol error ({type(exc).__name__})",
                retryable=False,
            ) from exc

    def close(self, *, force: bool = False) -> None:
        """Close the IMAP session cleanly (or immediately via shutdown when force=True)."""
        conn = self._conn
        self._conn = None
        self._capabilities = frozenset()
        _close_connection(conn, force=force)

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

            # Force-close the broken socket so the next attempt reconnects without waiting 30s.
            self.close(force=True)
            delay = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)  # noqa: S311
            logger.warning(
                "Retrying Yahoo IMAP %s (attempt %d/%d) after %.2fs",
                operation_name,
                attempt,
                _MAX_RETRIES,
                delay,
            )
            self._sleep(delay)

        assert last_exc is not None
        raise last_exc

    def ensure_archive_folder(self) -> None:
        """Create the destination folder on Yahoo if it does not already exist."""

        def _op(conn: imaplib.IMAP4_SSL) -> None:
            quoted_archive = _quote_mailbox(self._archive_folder)
            status, list_data = conn.list('""', quoted_archive)
            if status == "OK" and list_data and any(list_data):
                return

            create_status, _ = conn.create(quoted_archive)
            if create_status != "OK":
                # Re-check in case the server reported ALREADYEXISTS or similar.
                recheck_status, recheck_data = conn.list('""', quoted_archive)
                if recheck_status == "OK" and recheck_data and any(recheck_data):
                    return
                raise YahooImapError("Failed to create Yahoo archive folder", retryable=False)

        self._with_retry("ensure_archive_folder", _op)

    def list_recent_candidates(self, cutoff_utc: datetime) -> tuple[list[YahooCandidate], int, int]:
        """Return (eligible_candidates, total_scanned, skipped_too_old) from INBOX.

        Uses IMAP SINCE (date-only, with a 1-day safety buffer) followed by an exact
        UTC INTERNALDATE check so timezone differences never drop recent mail.
        """
        search_since = format_imap_date(cutoff_utc - timedelta(days=1))

        def _search_and_fetch_meta(
            conn: imaplib.IMAP4_SSL,
        ) -> tuple[list[YahooCandidate], int, int]:
            status, search_data = conn.uid("SEARCH", None, "SINCE", search_since)
            if status != "OK":
                raise YahooImapError("Yahoo IMAP UID SEARCH failed", retryable=True)

            uid_tokens: list[str] = []
            for raw_uids in search_data or []:
                if not raw_uids:
                    continue
                decoded = (
                    raw_uids.decode("ascii")
                    if isinstance(raw_uids, (bytes, bytearray))
                    else str(raw_uids)
                )
                uid_tokens.extend(decoded.split())

            if not uid_tokens:
                return [], 0, 0

            eligible: list[YahooCandidate] = []
            skipped_too_old = 0

            for start in range(0, len(uid_tokens), _FETCH_META_BATCH_SIZE):
                batch = uid_tokens[start : start + _FETCH_META_BATCH_SIZE]
                uid_set = ",".join(batch)
                f_status, fetch_data = conn.uid("FETCH", uid_set, "(UID INTERNALDATE RFC822.SIZE)")
                if f_status != "OK":
                    raise YahooImapError("Yahoo IMAP metadata FETCH failed", retryable=True)

                parsed_uids: set[str] = set()
                for entry in fetch_data or []:
                    raw_line = entry[0] if isinstance(entry, tuple) else entry
                    if not isinstance(raw_line, (bytes, bytearray)):
                        continue
                    candidate = _parse_metadata_item(bytes(raw_line))
                    if candidate is None:
                        continue
                    parsed_uids.add(candidate.uid)
                    if candidate.internal_date < cutoff_utc:
                        skipped_too_old += 1
                    else:
                        eligible.append(candidate)

                missing_uids = [uid for uid in batch if uid not in parsed_uids]
                if missing_uids:
                    logger.warning(
                        "Missing or unparseable IMAP metadata for %d UIDs: %s",
                        len(missing_uids),
                        ",".join(missing_uids),
                    )

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
        copied = False

        def _move(conn: imaplib.IMAP4_SSL) -> None:
            nonlocal copied
            store_status, _ = conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
            if store_status != "OK":
                raise YahooImapError(f"Failed to mark UID {uid} as read", retryable=True)

            if "MOVE" in self._capabilities:
                move_status, _ = conn.uid("MOVE", uid, target)
                if move_status != "OK":
                    raise YahooImapError(f"Yahoo IMAP MOVE failed for UID {uid}", retryable=True)
                return

            if not copied:
                copy_status, _ = conn.uid("COPY", uid, target)
                if copy_status != "OK":
                    raise YahooImapError(f"Yahoo IMAP COPY failed for UID {uid}", retryable=True)
                copied = True

            del_status, _ = conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
            if del_status != "OK":
                raise YahooImapError(f"Failed to mark UID {uid} deleted after COPY", retryable=True)

            if "UIDPLUS" in self._capabilities:
                exp_status, _ = conn.uid("EXPUNGE", uid)
            else:
                logger.warning(
                    "IMAP server lacks MOVE and UIDPLUS; falling back to mailbox-wide EXPUNGE "
                    "for UID %s",
                    uid,
                )
                exp_status, _ = conn.expunge()
            if exp_status != "OK":
                raise YahooImapError(f"Yahoo IMAP EXPUNGE failed for UID {uid}", retryable=True)

        self._with_retry(f"mark_read_and_move({uid})", _move)
