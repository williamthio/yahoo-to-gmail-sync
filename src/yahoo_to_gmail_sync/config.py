"""Configuration loading and validation for yahoo-to-gmail-sync."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_YAHOO_IMAP_HOST = "imap.mail.yahoo.com"
DEFAULT_YAHOO_IMAP_PORT = 993
DEFAULT_YAHOO_SOURCE_FOLDER = "INBOX"
DEFAULT_YAHOO_ARCHIVE_FOLDER = "Forwarded"
DEFAULT_MAX_AGE_HOURS = 48
DEFAULT_GMAIL_LABEL_NAME = "Yahoo"
DEFAULT_MAX_RUNTIME_SECONDS = 210

CREDENTIALS_JSON_ENV_VAR = "SYNC_CREDENTIALS_JSON"
REQUIRED_CREDENTIAL_KEYS = (
    "YAHOO_EMAIL",
    "YAHOO_APP_PASSWORD",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
)


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _get_or_default(source: Mapping[str, str], key: str, default: str) -> str:
    """Return a stripped env value, falling back to default when unset or blank."""
    value = source.get(key, "").strip()
    return value or default


def _validate_imap_folder(name: str, env_var: str) -> str:
    """Ensure IMAP mailbox names contain printable ASCII without control characters."""
    if any(ch in name for ch in ("\r", "\n")):
        raise ConfigError(f"{env_var} must not contain newline characters")
    if not name.isascii() or not name.isprintable():
        raise ConfigError(f"{env_var} must contain printable ASCII characters only")
    return name


def _parse_credentials_json(raw_json: str) -> dict[str, str]:
    """Parse SYNC_CREDENTIALS_JSON without leaking secret contents in errors."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CREDENTIALS_JSON_ENV_VAR} must be a valid JSON object") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(f"{CREDENTIALS_JSON_ENV_VAR} must be a JSON object")

    result: dict[str, str] = {}
    for key in REQUIRED_CREDENTIAL_KEYS:
        value = parsed.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ConfigError(f"{CREDENTIALS_JSON_ENV_VAR} field {key} must be a string")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Validated runtime configuration loaded from environment variables."""

    yahoo_email: str
    yahoo_app_password: str
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    yahoo_imap_host: str = DEFAULT_YAHOO_IMAP_HOST
    yahoo_imap_port: int = DEFAULT_YAHOO_IMAP_PORT
    yahoo_source_folder: str = DEFAULT_YAHOO_SOURCE_FOLDER
    yahoo_archive_folder: str = DEFAULT_YAHOO_ARCHIVE_FOLDER
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    gmail_label_name: str = DEFAULT_GMAIL_LABEL_NAME
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS

    def __repr__(self) -> str:
        # Redact all credentials and identifiers so tracebacks/logs stay safe.
        return (
            "SyncConfig("
            f"yahoo_imap_host={self.yahoo_imap_host!r}, "
            f"yahoo_imap_port={self.yahoo_imap_port!r}, "
            f"yahoo_source_folder={self.yahoo_source_folder!r}, "
            f"yahoo_archive_folder={self.yahoo_archive_folder!r}, "
            f"max_age_hours={self.max_age_hours!r}, "
            f"gmail_label_name={self.gmail_label_name!r}, "
            f"max_runtime_seconds={self.max_runtime_seconds!r}, "
            "credentials='<redacted>')"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SyncConfig:
        """Load and validate configuration from environment variables.

        Credentials should be provided via individual environment variables (recommended for
        GitHub Actions per-value secret masking) or optionally bundled into
        SYNC_CREDENTIALS_JSON. Individual environment variables take precedence when both are set.
        """
        source = os.environ if env is None else env

        credentials: dict[str, str] = {}
        raw_creds_json = source.get(CREDENTIALS_JSON_ENV_VAR, "").strip()
        if raw_creds_json:
            credentials.update(_parse_credentials_json(raw_creds_json))

        for key in REQUIRED_CREDENTIAL_KEYS:
            direct_val = source.get(key, "")
            if direct_val.strip():
                credentials[key] = direct_val

        missing = [
            name for name in REQUIRED_CREDENTIAL_KEYS if not credentials.get(name, "").strip()
        ]
        if missing:
            raise ConfigError(f"Missing required credentials: {', '.join(missing)}")

        port_raw = _get_or_default(source, "YAHOO_IMAP_PORT", str(DEFAULT_YAHOO_IMAP_PORT))
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError("YAHOO_IMAP_PORT must be a positive integer") from exc
        if port <= 0 or port > 65535:
            raise ConfigError("YAHOO_IMAP_PORT must be between 1 and 65535")

        max_age_raw = _get_or_default(source, "SYNC_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS))
        try:
            max_age_hours = int(max_age_raw)
        except ValueError as exc:
            raise ConfigError("SYNC_MAX_AGE_HOURS must be a positive integer") from exc
        if max_age_hours <= 0:
            raise ConfigError("SYNC_MAX_AGE_HOURS must be greater than 0")

        max_runtime_raw = _get_or_default(
            source, "SYNC_MAX_RUNTIME_SECONDS", str(DEFAULT_MAX_RUNTIME_SECONDS)
        )
        try:
            max_runtime_seconds = int(max_runtime_raw)
        except ValueError as exc:
            raise ConfigError("SYNC_MAX_RUNTIME_SECONDS must be a positive integer") from exc
        if max_runtime_seconds <= 0:
            raise ConfigError("SYNC_MAX_RUNTIME_SECONDS must be greater than 0")

        yahoo_imap_host = _get_or_default(source, "YAHOO_IMAP_HOST", DEFAULT_YAHOO_IMAP_HOST)
        yahoo_source_folder = _validate_imap_folder(
            _get_or_default(source, "YAHOO_SOURCE_FOLDER", DEFAULT_YAHOO_SOURCE_FOLDER),
            "YAHOO_SOURCE_FOLDER",
        )
        yahoo_archive_folder = _validate_imap_folder(
            _get_or_default(source, "YAHOO_ARCHIVE_FOLDER", DEFAULT_YAHOO_ARCHIVE_FOLDER),
            "YAHOO_ARCHIVE_FOLDER",
        )
        gmail_label_name = _get_or_default(source, "GMAIL_LABEL_NAME", DEFAULT_GMAIL_LABEL_NAME)

        if yahoo_source_folder.casefold() == yahoo_archive_folder.casefold():
            raise ConfigError("YAHOO_SOURCE_FOLDER and YAHOO_ARCHIVE_FOLDER must be different")

        return cls(
            yahoo_email=credentials["YAHOO_EMAIL"].strip(),
            yahoo_app_password=credentials["YAHOO_APP_PASSWORD"].strip("\r\n"),
            gmail_client_id=credentials["GMAIL_CLIENT_ID"].strip(),
            gmail_client_secret=credentials["GMAIL_CLIENT_SECRET"].strip(),
            gmail_refresh_token=credentials["GMAIL_REFRESH_TOKEN"].strip(),
            yahoo_imap_host=yahoo_imap_host,
            yahoo_imap_port=port,
            yahoo_source_folder=yahoo_source_folder,
            yahoo_archive_folder=yahoo_archive_folder,
            max_age_hours=max_age_hours,
            gmail_label_name=gmail_label_name,
            max_runtime_seconds=max_runtime_seconds,
        )
