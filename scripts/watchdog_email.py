"""Optional provider-neutral SMTP delivery for Gateway operational alerts."""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]
    security: str
    timeout_seconds: int

    @classmethod
    def from_environment(cls) -> SMTPSettings | None:
        host = os.getenv("IBKR_ALERT_SMTP_HOST", "").strip()
        recipient_text = os.getenv("IBKR_ALERT_TO", "").strip()
        if not host and not recipient_text:
            return None

        username = os.getenv("IBKR_ALERT_SMTP_USERNAME", "").strip()
        sender = os.getenv("IBKR_ALERT_FROM", "").strip()
        password_path_text = os.getenv(
            "IBKR_ALERT_SMTP_PASSWORD_FILE",
            "/etc/ibkr-gateway-watchdog/smtp-password",
        ).strip()
        missing = [
            name
            for name, value in (
                ("IBKR_ALERT_SMTP_HOST", host),
                ("IBKR_ALERT_SMTP_USERNAME", username),
                ("IBKR_ALERT_SMTP_PASSWORD_FILE", password_path_text),
                ("IBKR_ALERT_FROM", sender),
                ("IBKR_ALERT_TO", recipient_text),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Email alerts are partially configured; missing " + ", ".join(missing)
            )

        password_path = Path(password_path_text).expanduser()
        try:
            password = password_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(
                f"Could not read SMTP password file {password_path}: {error}"
            ) from error
        if not password:
            raise ValueError(f"SMTP password file is empty: {password_path}")

        recipients = tuple(
            address.strip() for address in recipient_text.split(",") if address.strip()
        )
        if not recipients:
            raise ValueError("IBKR_ALERT_TO must contain at least one email address")

        security = os.getenv("IBKR_ALERT_SMTP_SECURITY", "starttls").strip().lower()
        if security not in {"starttls", "ssl"}:
            raise ValueError("IBKR_ALERT_SMTP_SECURITY must be starttls or ssl")

        port = int(os.getenv("IBKR_ALERT_SMTP_PORT", "587"))
        timeout_seconds = int(os.getenv("IBKR_ALERT_SMTP_TIMEOUT_SECONDS", "10"))
        if port <= 0 or timeout_seconds <= 0:
            raise ValueError("SMTP port and timeout must be greater than zero")

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            sender=sender,
            recipients=recipients,
            security=security,
            timeout_seconds=timeout_seconds,
        )


class SMTPEmailSender:
    def __init__(self, settings: SMTPSettings):
        self.settings = settings

    async def send(self, subject: str, body: str) -> tuple[bool, str]:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._send_sync, subject, body),
                timeout=self.settings.timeout_seconds + 2,
            )
        except Exception as error:  # noqa: BLE001 - alert failures must be contained.
            return False, f"{type(error).__name__}: {error}"
        return True, "email accepted by SMTP server"

    def _send_sync(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.sender
        message["To"] = ", ".join(self.settings.recipients)
        message["Subject"] = subject
        message.set_content(body)

        context = ssl.create_default_context()
        if self.settings.security == "ssl":
            with smtplib.SMTP_SSL(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout_seconds,
                context=context,
            ) as smtp:
                smtp.login(self.settings.username, self.settings.password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(
            self.settings.host,
            self.settings.port,
            timeout=self.settings.timeout_seconds,
        ) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(self.settings.username, self.settings.password)
            smtp.send_message(message)


def email_sender_from_environment() -> SMTPEmailSender | None:
    settings = SMTPSettings.from_environment()
    return SMTPEmailSender(settings) if settings is not None else None
