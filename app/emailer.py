"""SMTP email sending - kept separate from main.py so checker.py can use
it too without creating a circular import (main.py already imports
checker.py). Credentials come from environment variables only, never the
database or a web form."""

from __future__ import annotations

import html
import os
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional

SMTP_HOST = os.environ.get("PUMA_SMTP_HOST")
SMTP_PORT = int(os.environ.get("PUMA_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("PUMA_SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("PUMA_SMTP_PASSWORD")
SMTP_FROM_ADDRESS = os.environ.get("PUMA_SMTP_FROM_ADDRESS", SMTP_USERNAME)
SMTP_FROM_NAME = os.environ.get("PUMA_SMTP_FROM_NAME", "PUMA Notifications")
SMTP_USE_STARTTLS = os.environ.get("PUMA_SMTP_USE_STARTTLS", "true").lower() == "true"
SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)

# The crest logo, embedded directly in the email (not linked by URL) since
# PUMA only lives on the internal network - an external mail client has no
# way to fetch an image from an address it can't reach. Embedding travels
# with the message itself, so it works regardless of the recipient's
# network access.
LOGO_PATH = Path(__file__).resolve().parent / "static" / "img" / "logo-login.png"
LOGO_CONTENT_ID = "puma-logo"


def _build_html_body(body: str, include_logo: bool) -> str:
    """Wraps plain body text in a simple HTML shell with inline styles -
    inline is the only styling approach email clients reliably support,
    a <style> block gets stripped by plenty of them. Escapes the text
    first since this becomes real markup."""
    escaped = html.escape(body).replace("\n", "<br>")
    logo_html = (
        f'<img src="cid:{LOGO_CONTENT_ID}" width="56" height="56" '
        'style="display:block; margin:0 auto 12px;" alt="PUMA">'
        if include_logo
        else ""
    )
    return f"""\
<html>
  <body style="margin:0; padding:24px; background-color:#f5f5f7;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" style="max-width:520px;" cellpadding="0" cellspacing="0">
            <tr>
              <td align="center" style="background-color:#ffffff; border-radius:12px; padding:32px; font-family:Arial, Helvetica, sans-serif; font-size:17px; line-height:1.6; color:#1f2937;">
                {logo_html}
                <div style="color:#e82a78; font-weight:bold; font-size:20px; margin-bottom:16px;">PUMA</div>
                <div style="text-align:left; width:100%;">{escaped}</div>
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
    plain-text default, with the PUMA crest embedded inline) - plain text
    stays as a fallback for anything that can't render HTML. Returns
    (success, error_message) rather than raising - callers (the checker's
    update-detection loop, the Settings test-email button) both need this
    to fail quietly without taking anything else down with it."""
    if not SMTP_CONFIGURED:
        return False, "SMTP is not configured (missing host, username, or password in puma.env)."
    if not to_addresses:
        return False, "No recipient email address is configured."

    logo_bytes = None
    if LOGO_PATH.exists():
        try:
            logo_bytes = LOGO_PATH.read_bytes()
        except OSError:
            logo_bytes = None  # missing/unreadable logo shouldn't block sending

    msg_root = MIMEMultipart("related")
    msg_root["Subject"] = subject
    msg_root["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_ADDRESS))
    msg_root["To"] = ", ".join(to_addresses)

    msg_alternative = MIMEMultipart("alternative")
    msg_alternative.attach(MIMEText(body, "plain"))
    msg_alternative.attach(MIMEText(_build_html_body(body, include_logo=bool(logo_bytes)), "html"))
    msg_root.attach(msg_alternative)

    if logo_bytes:
        logo_part = MIMEImage(logo_bytes)
        logo_part.add_header("Content-ID", f"<{LOGO_CONTENT_ID}>")
        logo_part.add_header("Content-Disposition", "inline", filename="puma-logo.png")
        msg_root.attach(logo_part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            if SMTP_USE_STARTTLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_ADDRESS, to_addresses, msg_root.as_string())
        return True, None
    except Exception as e:
        return False, str(e)
