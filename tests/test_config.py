"""Tests for configuration loading and secret redaction."""

from __future__ import annotations

import pytest

from yahoo_to_gmail_sync.config import ConfigError, SyncConfig


def _valid_env() -> dict[str, str]:
    return {
        "YAHOO_EMAIL": "user@yahoo.com",
        "YAHOO_APP_PASSWORD": "super-secret-yahoo-pw",
        "GMAIL_CLIENT_ID": "client-id.apps.googleusercontent.com",
        "GMAIL_CLIENT_SECRET": "super-secret-gmail-cs",
        "GMAIL_REFRESH_TOKEN": "super-secret-refresh-token",
    }


def test_from_env_defaults() -> None:
    cfg = SyncConfig.from_env(_valid_env())
    assert cfg.yahoo_imap_host == "imap.mail.yahoo.com"
    assert cfg.yahoo_imap_port == 993
    assert cfg.yahoo_source_folder == "INBOX"
    assert cfg.yahoo_archive_folder == "Forwarded"
    assert cfg.max_age_hours == 48
    assert cfg.gmail_label_name == "Yahoo"


def test_repr_redacts_secrets() -> None:
    cfg = SyncConfig.from_env(_valid_env())
    rendered = repr(cfg)
    for secret in _valid_env().values():
        assert secret not in rendered
    assert "<redacted>" in rendered


def test_missing_required_env_raises() -> None:
    env = _valid_env()
    del env["GMAIL_REFRESH_TOKEN"]
    with pytest.raises(ConfigError, match="GMAIL_REFRESH_TOKEN"):
        SyncConfig.from_env(env)


def test_invalid_port_or_age_raises() -> None:
    env = _valid_env()
    env["YAHOO_IMAP_PORT"] = "not-an-int"
    with pytest.raises(ConfigError, match="YAHOO_IMAP_PORT"):
        SyncConfig.from_env(env)

    env = _valid_env()
    env["SYNC_MAX_AGE_HOURS"] = "0"
    with pytest.raises(ConfigError, match="SYNC_MAX_AGE_HOURS"):
        SyncConfig.from_env(env)
