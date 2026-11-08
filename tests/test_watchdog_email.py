from pathlib import Path

import pytest

from scripts.watchdog_email import SMTPSettings


def test_email_is_optional(monkeypatch):
    monkeypatch.delenv("IBKR_ALERT_SMTP_HOST", raising=False)
    monkeypatch.delenv("IBKR_ALERT_TO", raising=False)

    assert SMTPSettings.from_environment() is None


def test_generic_smtp_settings_load_password_from_file(monkeypatch, tmp_path: Path):
    password_file = tmp_path / "smtp-password"
    password_file.write_text("secret-password\n", encoding="utf-8")
    monkeypatch.setenv("IBKR_ALERT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("IBKR_ALERT_SMTP_USERNAME", "alerts@example.com")
    monkeypatch.setenv("IBKR_ALERT_SMTP_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("IBKR_ALERT_FROM", "Server <alerts@example.com>")
    monkeypatch.setenv("IBKR_ALERT_TO", "one@example.com, two@example.com")

    settings = SMTPSettings.from_environment()

    assert settings is not None
    assert settings.password == "secret-password"
    assert settings.recipients == ("one@example.com", "two@example.com")
    assert settings.security == "starttls"


def test_partial_email_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("IBKR_ALERT_SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("IBKR_ALERT_TO", raising=False)

    with pytest.raises(ValueError, match="partially configured"):
        SMTPSettings.from_environment()
