"""Tests for configuration loading and secret redaction."""

from __future__ import annotations

import json

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


def test_from_env_supports_single_json_secret_and_env_override() -> None:
    env = {
        "SYNC_CREDENTIALS_JSON": json.dumps(_valid_env()),
        "GMAIL_LABEL_NAME": "  ",  # Blank optional override falls back to default.
        "SYNC_MAX_AGE_HOURS": "",
    }
    cfg = SyncConfig.from_env(env)
    assert cfg.yahoo_email == "user@yahoo.com"
    assert cfg.gmail_refresh_token == "super-secret-refresh-token"
    assert cfg.gmail_label_name == "Yahoo"
    assert cfg.max_age_hours == 48

    # Direct env var overrides the JSON bundle when both are provided.
    env["YAHOO_EMAIL"] = "override@yahoo.com"
    cfg_override = SyncConfig.from_env(env)
    assert cfg_override.yahoo_email == "override@yahoo.com"


def test_invalid_credentials_json_does_not_leak_payload() -> None:
    env = {"SYNC_CREDENTIALS_JSON": '{"YAHOO_APP_PASSWORD": "leaked-secret"'}
    with pytest.raises(ConfigError) as exc_info:
        SyncConfig.from_env(env)
    assert "leaked-secret" not in str(exc_info.value)


def test_app_password_strips_trailing_newlines() -> None:
    env = _valid_env()
    env["YAHOO_APP_PASSWORD"] = "super-secret-yahoo-pw\r\n"
    cfg = SyncConfig.from_env(env)
    assert cfg.yahoo_app_password == "super-secret-yahoo-pw"


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


def test_invalid_port_age_or_same_folders_raises() -> None:
    env = _valid_env()
    env["YAHOO_IMAP_PORT"] = "not-an-int"
    with pytest.raises(ConfigError, match="YAHOO_IMAP_PORT"):
        SyncConfig.from_env(env)

    env = _valid_env()
    env["SYNC_MAX_AGE_HOURS"] = "0"
    with pytest.raises(ConfigError, match="SYNC_MAX_AGE_HOURS"):
        SyncConfig.from_env(env)

    env = _valid_env()
    env["YAHOO_SOURCE_FOLDER"] = "INBOX"
    env["YAHOO_ARCHIVE_FOLDER"] = "inbox"
    with pytest.raises(ConfigError, match="must be different"):
        SyncConfig.from_env(env)
