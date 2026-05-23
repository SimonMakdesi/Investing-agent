"""Email delivery via Gmail SMTP.

Gmail requires an App Password (not your normal password). Generate one
at myaccount.google.com/apppasswords and put it in .env as GMAIL_APP_PASSWORD.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from src.config import settings

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465  # SSL


def send_email(subject: str, body_markdown: str, recipient: str | None = None) -> None:
    """Send a plain-text + markdown email. Raises on failure."""
    settings.require("gmail_address", "gmail_app_password")
    to_addr = recipient or settings.report_recipient or settings.gmail_address
    if not to_addr:
        raise RuntimeError("No recipient specified and REPORT_RECIPIENT is empty")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.gmail_address
    msg["To"] = to_addr
    msg.set_content(body_markdown)
    # A trivial HTML alternative so most clients render line breaks; we keep it
    # minimal because the report is designed to be readable as plain text.
    html_body = f"<pre style='font-family: ui-monospace, Menlo, monospace; white-space: pre-wrap;'>{_escape_html(body_markdown)}</pre>"
    msg.add_alternative(html_body, subtype="html")

    log.info("Sending email to %s (subject: %s)", to_addr, subject)
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=30) as smtp:
        smtp.login(settings.gmail_address, settings.gmail_app_password)
        smtp.send_message(msg)
    log.info("Email sent")


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
