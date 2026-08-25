"""Email delivery (SMTP) — invoices, receipts, SkillsFuture/PayNow instructions,
staff alerts and the nightly accounts report.

If EMAIL_ENABLED is false, emails are logged to the console and not sent, so the
full pipeline can be demoed without an SMTP account.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)


def send_email(
    to: str,
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
) -> bool:
    attachments = attachments or []
    if not settings.email_enabled:
        names = ", ".join(p.name for p in attachments) or "none"
        print(
            f"\n[EMAIL → {to}] (disabled, not sent)\n"
            f"Subject: {subject}\nAttachments: {names}\n{'-'*50}\n{body}\n{'-'*50}"
        )
        return True

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    for path in attachments:
        try:
            data = Path(path).read_bytes()
            msg.add_attachment(
                data, maintype="application", subtype="pdf", filename=Path(path).name
            )
        except Exception as e:  # noqa: BLE001
            log.error("attach failed %s: %s", path, e)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_use_tls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        log.info("Email sent to %s (%s)", to, subject)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Email send failed to %s: %s", to, e)
        return False
