"""SMTP email sending - kept separate from main.py so checker.py can use
it too without creating a circular import (main.py already imports
checker.py). Credentials come from environment variables only, never the
database or a web form."""

from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from typing import Optional

SMTP_HOST = os.environ.get("PUMA_SMTP_HOST")
SMTP_PORT = int(os.environ.get("PUMA_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("PUMA_SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("PUMA_SMTP_PASSWORD")
SMTP_FROM_ADDRESS = os.environ.get("PUMA_SMTP_FROM_ADDRESS", SMTP_USERNAME)
SMTP_USE_STARTTLS = os.environ.get("PUMA_SMTP_USE_STARTTLS", "true").lower() == "true"
SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)


def send_email(to_addresses: list[str], subject: str, body: str) -> tuple[bool, Optional[str]]:
    """Send a plain-text email via the configured SMTP relay. Returns
    (success, error_message) rather than raising - callers (the checker's
    update-detection loop, the Settings test-email button) both need this
    to fail quietly without taking anything else down with it."""
    if not SMTP_CONFIGURED:
        return False, "SMTP is not configured (missing host, username, or password in puma.env)."
    if not to_addresses:
        return False, "No recipient email address is configured."

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_ADDRESS
    msg["To"] = ", ".join(to_addresses)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            if SMTP_USE_STARTTLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_ADDRESS, to_addresses, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)
