"""SMTP email sending - kept separate from main.py so checker.py can use
it too without creating a circular import (main.py already imports
checker.py). Credentials come from environment variables only, never the
database or a web form."""

from __future__ import annotations

import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

SMTP_HOST = os.environ.get("PUMA_SMTP_HOST")
SMTP_PORT = int(os.environ.get("PUMA_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("PUMA_SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("PUMA_SMTP_PASSWORD")
SMTP_FROM_ADDRESS = os.environ.get("PUMA_SMTP_FROM_ADDRESS", SMTP_USERNAME)
SMTP_FROM_NAME = os.environ.get("PUMA_SMTP_FROM_NAME", "PUMA Notifications")
SMTP_USE_STARTTLS = os.environ.get("PUMA_SMTP_USE_STARTTLS", "true").lower() == "true"
SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)


def _build_html_body(body: str) -> str:
    """Wraps plain body text in a simple HTML shell with inline styles -
    inline is the only styling approach email clients reliably support,
    a <style> block gets stripped by plenty of them. Escapes the text
    first since this becomes real markup."""
    escaped = html.escape(body).replace("\n", "<br>")
    return f"""\
<html>
  <body style="margin:0; padding:24px; background-color:#f5f5f7;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" style="max-width:520px;" cellpadding="0" cellspacing="0">
            <tr>
              <td style="background-color:#ffffff; border-radius:12px; padding:32px; font-family:Arial, Helvetica, sans-serif; font-size:17px; line-height:1.6; color:#1f2937;">
                <div style="color:#e82a78; font-weight:bold; font-size:20px; margin-bottom:16px;">PUMA</div>
                {escaped}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_email(to_addresses: list[str], subject: str, body: str) -> tuple[bool, Optional[str]]:
    """Send an email via the configured SMTP relay. Sends both a plain-text
    and an HTML part (the HTML one is what actually shows in modern email
    clients, styled with a larger, friendlier font than any bare
    plain-text default) - plain text stays as a fallback for anything
    that can't render HTML. Returns (success, error_message) rather than
    raising - callers (the checker's update-detection loop, the Settings
    test-email button) both need this to fail quietly without taking
    anything else down with it."""
    if not SMTP_CONFIGURED:
        return False, "SMTP is not configured (missing host, username, or password in puma.env)."
    if not to_addresses:
        return False, "No recipient email address is configured."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_ADDRESS))
    msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(_build_html_body(body), "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            if SMTP_USE_STARTTLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_ADDRESS, to_addresses, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)
