"""SMTP email sender + Jinja2 template renderer for deployment notifications.

Sits in the backend (not the worker) because the listener that picks up
``task-succeeded`` already lives there and has DB access for resolving
user emails. The worker stays infrastructure-only.

Sending strategy:

* Gmail SMTP via ``settings.SMTP_*`` — App password required when 2FA is
  on. The kill-switch is ``settings.SMTP_ENABLED`` (off by default); set
  it to ``True`` and provide ``SMTP_USER`` / ``SMTP_PASSWORD`` to turn
  delivery on. With either condition missing, ``send_email`` becomes a
  no-op and returns ``False`` without raising.
* Port-aware connection: ``465`` opens an implicit-TLS connection
  (``SMTP_SSL``); anything else (typically ``587``) uses ``SMTP`` and
  upgrades via STARTTLS. Some corporate networks (SAP intranet
  included) block outbound 587 but allow 465 — switching is a config
  change, no code edit.
* MIME ``multipart/alternative`` with both an HTML and a plain-text
  body so clients without HTML rendering still see something legible.
  Templates live next to this file in ``templates/email/``.
* Each ``send_email`` is its own SMTP connection (no pooling). Gmail
  drops idle connections aggressively and the volume here is low —
  one mail per user per deploy. Reuse would just add reconnect-on-
  expired complexity.

Failures are logged at ``warning`` and swallowed: a failed mail must
never roll back a successful deployment. The listener checks the
return value if it wants to surface a "mail not sent" badge.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

logger = logging.getLogger(__name__)


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "email"

# HTML templates: autoescape, trim/lstrip blocks for clean indentation.
_jinja_html = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Plain-text templates: no autoescape (escaping ``&`` inside passwords
# would mangle them), and no block stripping — Jinja's whitespace
# control removes critical newlines around ``{% if %}`` / ``{% for %}``
# blocks in plain text. Authors use ``{%- ... -%}`` explicitly when
# they want trimming.
_jinja_text = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=True,
)


def render(template_name: str, **context: Any) -> str:
    """Render a Jinja2 template with the given context.

    Picks the HTML or text environment based on the template's
    extension so plain-text templates aren't HTML-escaped and
    HTML templates still get tidy whitespace.
    """
    env = _jinja_text if template_name.endswith(".txt") else _jinja_html
    return env.get_template(template_name).render(**context)


def is_smtp_enabled() -> bool:
    """Effective SMTP availability — the predicate every caller should use.

    Both conditions must hold for mail delivery to even be attempted:
      * ``SMTP_ENABLED`` is the explicit operator kill-switch (default
        ``False``). It exists so a dev / CI environment can leave the
        Gmail app-password in ``.env`` for later but keep delivery off.
      * ``SMTP_USER`` and ``SMTP_PASSWORD`` must be populated.
        ``SMTP_ENABLED=True`` with empty credentials is treated as
        "configuration in progress" and still skips delivery — better
        than crashing at submit-time with an auth error.

    Returning a single boolean lets the resend-access endpoint
    short-circuit BEFORE accessing the deployment / notifier pipeline,
    so a 503 response carries the right semantic ("we chose not to
    send") instead of leaking a 502 ("we tried and failed") when the
    cause is purely a configuration choice.
    """
    return bool(
        settings.SMTP_ENABLED
        and settings.SMTP_USER
        and settings.SMTP_PASSWORD
    )


def send_email(
    *,
    to: str | list[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> bool:
    """Send a multipart HTML+text email via Gmail SMTP.

    Returns ``True`` on success, ``False`` if SMTP isn't configured or
    sending raised. Never raises — the deployment notification flow
    must keep going even if mail is broken.
    """
    if not settings.SMTP_ENABLED:
        logger.info("SMTP disabled (SMTP_ENABLED=false), skipping email to %s", to)
        return False
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.info("SMTP not configured (SMTP_USER empty), skipping email to %s", to)
        return False

    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        return False

    from_email = settings.SMTP_FROM_EMAIL or settings.SMTP_USER

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{from_email}>"
    msg["To"] = ", ".join(recipients)
    # Plain part first so MIME-spec-compliant clients prefer the HTML
    # part (they pick the *last* alternative they can render).
    msg.attach(MIMEText(text_body, "plain", _charset="utf-8"))
    msg.attach(MIMEText(html_body, "html", _charset="utf-8"))

    try:
        # 30s timeout: Gmail's STARTTLS handshake can take 10-15s on
        # first contact. Port 465 = implicit TLS (SMTPS); anything else
        # = STARTTLS. Gmail accepts either.
        if settings.SMTP_PORT == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=30, context=ctx,
            ) as smtp:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.sendmail(from_email, recipients, msg.as_string())
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.sendmail(from_email, recipients, msg.as_string())
        logger.info("Sent email to %s — subject=%r", recipients, subject)
        return True
    except Exception as e:
        # Log full message so the operator can diagnose (auth fail vs.
        # timeout vs. blocked recipient) without re-running the mail.
        logger.warning("Failed to send email to %s: %s", recipients, e)
        return False
