"""Configuration loading and validation for yahoo-to-gmail-sync."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_YAHOO_IMAP_HOST = "imap.mail.yahoo.com"
DEFAULT_YAHOO_IMAP_PORT = 993
DEFAULT_YAHOO_SOURCE_FOLDER = "INBOX"
DEFAULT_YAHOO_ARCHIVE_FOLDER = "Forwarded"
DEFAULT_MAX_AGE_HOURS = 48
DEFAULT_GMAIL_LABEL_NAME = "Yahoo"

REQUIRED_ENV_VARS = (
    "YAHOO_EMAIL",
    "YAHOO_APP_PASSWORD",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
)


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Validated runtime configuration loaded from environment variables."""

    yahoo_email: str = ""
    yahoo_app_password: str = ""
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    yahoo_imap_host: str = DEFAULT_YAHOO_IMAP_HOST
    yahoo_imap_port: int = DEFAULT_YAHOO_IMAP_PORT
    yahoo_source_folder: str = DEFAULT_YAHOO_SOURCE_FOLDER
    yahoo_archive_folder: str = DEFAULT_YAHOO_ARCHIVE_FOLDER
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    gmail_label_name: str = DEFAULT_GMAIL_LABEL_NAME

    def __repr__(self) -> str:
        # Redact all credentials and identifiers so tracebacks/logs stay safe in public repos.
        return (
            "SyncConfig("
            f"yahoo_imap_host={self.yahoo_imap_host!r}, "
            f"yahoo_imap_port={self.yahoo_imap_port!r}, "
            f"yahoo_source_folder={self.yahoo_source_folder!r}, "
            f"yahoo_archive_folder={self.yahoo_archive_folder!r}, "
            f"max_age_hours={self.max_age_hours!r}, "
            f"gmail_label_name={self.gmail_label_name!r}, "
            "credentials='<redacted>')"
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SyncConfig:
        """Load and validate configuration from environment variables."""
        source = os.environ if env is None else env

        missing = [name for name in REQUIRED_ENV_VARS if not source.get(name, "").strip()]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        port_raw = source.get("YAHOO_IMAP_PORT", str(DEFAULT_YAHOO_IMAP_PORT)).strip()
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError("YAHOO_IMAP_PORT must be a positive integer") from exc
        if port <= 0 or port > 65535:
            raise ConfigError("YAHOO_IMAP_PORT must be between 1 and 65535")

        max_age_raw = source.get("SYNC_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)).strip()
        try:
            max_age_hours = int(max_age_raw)
        except ValueError as exc:
            raise ConfigError("SYNC_MAX_AGE_HOURS must be a positive integer") from exc
        if max_age_hours <= 0:
            raise ConfigError("SYNC_MAX_AGE_HOURS must be greater than 0")

        yahoo_imap_host = source.get("YAHOO_IMAP_HOST", DEFAULT_YAHOO_IMAP_HOST).strip()
        yahoo_source_folder = source.get("YAHOO_SOURCE_FOLDER", DEFAULT_YAHOO_SOURCE_FOLDER).strip()
        yahoo_archive_folder = source.get(
            "YAHOO_ARCHIVE_FOLDER", DEFAULT_YAHOO_ARCHIVE_FOLDER
        ).strip()
        gmail_label_name = source.get("GMAIL_LABEL_NAME", DEFAULT_GMAIL_LABEL_NAME).strip()

        if not yahoo_imap_host:
            raise ConfigError("YAHOO_IMAP_HOST must not be empty")
        if not yahoo_source_folder:
            raise ConfigError("YAHOO_SOURCE_FOLDER must not be empty")
        if not yahoo_archive_folder:
            raise ConfigError("YAHOO_ARCHIVE_FOLDER must not be empty")
        if not gmail_label_name:
            raise ConfigError("GMAIL_LABEL_NAME must not be empty")

        return cls(
            yahoo_email=source["YAHOO_EMAIL"].strip(),
            yahoo_app_password=source["YAHOO_APP_PASSWORD"],
            gmail_client_id=source["GMAIL_CLIENT_ID"].strip(),
            gmail_client_secret=source["GMAIL_CLIENT_SECRET"].strip(),
            gmail_refresh_token=source["GMAIL_REFRESH_TOKEN"].strip(),
            yahoo_imap_host=yahoo_imap_host,
            yahoo_imap_port=port,
            yahoo_source_folder=yahoo_source_folder,
            yahoo_archive_folder=yahoo_archive_folder,
            max_age_hours=max_age_hours,
            gmail_label_name=gmail_label_name,
        )
