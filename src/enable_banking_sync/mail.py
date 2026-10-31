"""E-Mail-Benachrichtigung an den Notfallkontakt (EB_ALERT_EMAIL) über SMTP."""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import urlsplit

from .config import Config, ConfigError

TIMEOUT = 30

# Fehler, die beim Versand auftreten können (Verbindung, TLS, Anmeldung, Ablehnung).
MAIL_ERRORS = (smtplib.SMTPException, OSError, ConfigError)


def send(cfg: Config, subject: str, body: str) -> str:
    """Versendet eine Nachricht an den Notfallkontakt und gibt die Empfänger zurück."""
    if not cfg.mail_configured:
        raise ConfigError("Der Mailversand ist nicht eingerichtet (EB_SMTP_HOST, EB_SMTP_FROM bzw. "
                          "EB_SMTP_USER und EB_ALERT_EMAIL setzen).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from or cfg.smtp_user
    msg["To"] = ", ".join(cfg.alert_email)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    context = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT, context=context)
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT)
    with smtp:
        if cfg.smtp_security == "starttls":
            smtp.starttls(context=context)
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password or "")
        smtp.send_message(msg)
    return msg["To"]


def public_url(redirect_url: str | None) -> str | None:
    """Öffentliche Adresse der Anwendung, abgeleitet aus der Redirect-URL."""
    parts = urlsplit(redirect_url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def reauth_message(bank: str | None, reason: str, base_url: str | None) -> tuple[str, str]:
    link = (f"Bitte die Bank in der Weboberfläche neu verbinden:\n{base_url}/connect\n" if base_url
            else "Bitte die Bank in der Weboberfläche unter „Bank verbinden“ neu verbinden.\n")
    body = (
        "Hallo,\n\n"
        "der automatische Abruf der Kontoumsätze wurde gestoppt, weil die Bank eine erneute "
        "Anmeldung verlangt.\n\n"
        f"Bank:      {bank or 'unbekannt'}\n"
        f"Zeitpunkt: {datetime.now().astimezone():%d.%m.%Y %H:%M}\n"
        f"Meldung:   {reason}\n\n"
        "Bis die Bank neu verbunden oder der Abruf in der Weboberfläche wieder freigegeben wird, "
        "unternimmt die Anwendung keine weiteren Abrufversuche.\n\n"
        f"{link}\n"
        "Diese Nachricht wurde automatisch von enable-banking-sync versendet.\n"
    )
    return "Enable Banking: Bitte Bank neu verbinden", body


def test_message(base_url: str | None) -> tuple[str, str]:
    body = (
        "Hallo,\n\n"
        "dies ist eine Test-Nachricht von enable-banking-sync. Der Mailversand funktioniert.\n\n"
        "An diese Adresse geht eine Nachricht, wenn die Bank eine erneute Anmeldung verlangt "
        "und der automatische Abruf deshalb gestoppt wird.\n"
        + (f"\nWeboberfläche: {base_url}/\n" if base_url else "")
    )
    return "Enable Banking: Test-Nachricht", body
