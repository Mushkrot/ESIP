"""Email notification service for ESIP alerts and report delivery.

Primary transport is Gmail API with Google Workspace domain-wide delegation as
morgan.chen@northstar.example.com. The Workspace Admin Console must authorize the
the configured reference DWD client ID for this narrow scope:

    https://www.googleapis.com/auth/gmail.send

SendGrid is kept only as an explicit legacy/fallback transport.

Recipients are managed via Firestore system/notification_config.recipients
and can be updated through the Admin Panel without redeployment.

Usage:
    from app.services.email_service import send_alert
    send_alert("Soft cap reached", "Today's spend: $7.20 / $10.00 limit", level="warning")
"""

import logging
import json
import base64
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import google.auth.transport.requests as google_requests_transport
import requests
from google.oauth2 import service_account

from app.config import settings
from app.reference_data.fixtures import PLATFORM_ADMIN_EMAIL, SUPPORT_ADMIN_EMAIL

logger = logging.getLogger(__name__)

BUSINESS_TZ = ZoneInfo("America/New_York")
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# Cache recipients list for 60s to avoid Firestore read on every alert.
_recipients_cache: Optional[list[str]] = None
_recipients_cache_ts: float = 0.0
_RECIPIENTS_CACHE_TTL = 60.0

_DEFAULT_RECIPIENTS = [
    SUPPORT_ADMIN_EMAIL,
    PLATFORM_ADMIN_EMAIL,
]

# Feedback is an operator workflow, not a platform-alert distribution. Keep its
# recipient intentionally separate so a new feedback entry reaches its owner
# without widening the cost/security alert mailing list.
_FEEDBACK_NOTIFICATION_RECIPIENT = SUPPORT_ADMIN_EMAIL

LEVEL_SUBJECTS = {
    # Keep the legacy [ESIP] prefix for inbox filter continuity.
    "info":     "[ESIP] Info",
    "warning":  "[ESIP] ⚠️ Warning",
    "critical": "[ESIP] 🚨 CRITICAL",
}


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    media_type: str = "application/octet-stream"


def _get_recipients() -> list[str]:
    """Read recipient list from Firestore system/notification_config (cached 60s)."""
    import time  # noqa: PLC0415
    global _recipients_cache, _recipients_cache_ts
    now = time.monotonic()
    if _recipients_cache is not None and (now - _recipients_cache_ts) < _RECIPIENTS_CACHE_TTL:
        return _recipients_cache
    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        doc = get_db().document("system/notification_config").get()
        data = doc.to_dict() if doc.exists else {}
        recipients = data.get("recipients") or _DEFAULT_RECIPIENTS
        _recipients_cache = [r for r in recipients if r and "@" in r]
        _recipients_cache_ts = now
        return _recipients_cache
    except Exception as exc:
        logger.warning("email_service: could not read recipients from Firestore: %s", exc)
        return _recipients_cache or _DEFAULT_RECIPIENTS


def _normalize_recipients(recipients: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for recipient in recipients:
        email = str(recipient or "").strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def _sender_email() -> str:
    return (
        settings.gmail_sender_email
        or settings.gmail_delegated_subject
        or settings.alert_from_email
        or PLATFORM_ADMIN_EMAIL
    ).strip()


def _sender_name() -> str:
    return (settings.gmail_sender_name or "ESIP Alerts").strip() or "ESIP Alerts"


def _build_mime_message(
    *,
    recipients: Sequence[str],
    cc_recipients: Sequence[str] | None = None,
    subject: str,
    body: str,
    attachments: Sequence[EmailAttachment] | None = None,
) -> EmailMessage:
    sender = _sender_email()
    normalized_to = _normalize_recipients(recipients)
    normalized_cc = [item for item in _normalize_recipients(cc_recipients or ()) if item not in set(normalized_to)]
    msg = EmailMessage()
    msg["To"] = ", ".join(normalized_to)
    if normalized_cc:
        msg["Cc"] = ", ".join(normalized_cc)
    msg["From"] = formataddr((_sender_name(), sender))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[-1])
    msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(body)

    for attachment in attachments or ():
        media_type = attachment.media_type or mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"
        maintype, subtype = media_type.split("/", 1) if "/" in media_type else ("application", "octet-stream")
        msg.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return msg


def _get_gmail_credentials() -> service_account.Credentials:
    if not settings.google_application_credentials:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is required for Gmail API domain-wide delegation")
    subject = (settings.gmail_delegated_subject or _sender_email()).strip()
    if not subject:
        raise RuntimeError("GMAIL_DELEGATED_SUBJECT is required for Gmail API sending")
    creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=[GMAIL_SEND_SCOPE],
    )
    return creds.with_subject(subject)


def _send_via_gmail(
    *,
    recipients: Sequence[str],
    cc_recipients: Sequence[str] | None = None,
    subject: str,
    body: str,
    attachments: Sequence[EmailAttachment] | None = None,
) -> bool:
    normalized = _normalize_recipients(recipients)
    normalized_cc = [item for item in _normalize_recipients(cc_recipients or ()) if item not in set(normalized)]
    if not normalized:
        logger.warning("email_service: no recipients configured — skipping Gmail send")
        return False

    try:
        creds = _get_gmail_credentials()
        creds.refresh(google_requests_transport.Request())
        message = _build_mime_message(
            recipients=normalized,
            cc_recipients=normalized_cc,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        payload = {
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii"),
        }
        resp = requests.post(
            GMAIL_SEND_URL,
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("email_service: Gmail sent to %d To and %d Cc recipients: %s", len(normalized), len(normalized_cc), subject)
            return True
        logger.error("email_service: Gmail HTTP %d: %s", resp.status_code, resp.text[:300])
        return False
    except Exception as exc:
        logger.error("email_service: Gmail send failed: %s", exc)
        return False


def _send_via_sendgrid(
    *,
    recipients: Sequence[str],
    cc_recipients: Sequence[str] | None = None,
    subject: str,
    body: str,
    attachments: Sequence[EmailAttachment] | None = None,
) -> bool:
    if not settings.sendgrid_api_key:
        logger.warning("email_service: SENDGRID_API_KEY not set — skipping SendGrid email")
        return False

    normalized = _normalize_recipients(recipients)
    normalized_cc = [item for item in _normalize_recipients(cc_recipients or ()) if item not in set(normalized)]
    if not normalized:
        logger.warning("email_service: no recipients configured — skipping SendGrid email")
        return False

    personalization: dict[str, object] = {"to": [{"email": r} for r in normalized]}
    if normalized_cc:
        personalization["cc"] = [{"email": r} for r in normalized_cc]
    payload: dict[str, object] = {
        "personalizations": [personalization],
        "from": {"email": settings.alert_from_email, "name": _sender_name()},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    if attachments:
        payload["attachments"] = [
            {
                "content": base64.b64encode(attachment.content).decode("ascii"),
                "filename": attachment.filename,
                "type": attachment.media_type,
                "disposition": "attachment",
            }
            for attachment in attachments
        ]

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=data,
        headers={
            "Authorization": f"Bearer {settings.sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - req uses the fixed HTTPS endpoint above.
            if resp.status == 202:
                logger.info("email_service: SendGrid sent to %d To and %d Cc recipients: %s", len(normalized), len(normalized_cc), subject)
                return True
            logger.warning("email_service: unexpected status %d from SendGrid", resp.status)
            return False
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:300]
        logger.error("email_service: SendGrid HTTP %d: %s", exc.code, error_body)
        return False
    except Exception as exc:
        logger.error("email_service: failed to send SendGrid email: %s", exc)
        return False


def send_email(
    *,
    recipients: Sequence[str],
    cc_recipients: Sequence[str] | None = None,
    subject: str,
    body: str,
    attachments: Sequence[EmailAttachment] | None = None,
) -> bool:
    """Send one plain-text email, optionally with attachments."""
    normalized = _normalize_recipients(recipients)
    normalized_cc = [item for item in _normalize_recipients(cc_recipients or ()) if item not in set(normalized)]
    if not normalized:
        logger.warning("email_service: no recipients configured — skipping email")
        return False

    transport = (settings.email_transport or "gmail_api").strip().lower()
    if transport in {"gmail", "gmail_api", "google"}:
        sent = _send_via_gmail(
            recipients=normalized,
            cc_recipients=normalized_cc,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        if sent or not settings.email_sendgrid_fallback_enabled:
            return sent
        logger.warning("email_service: Gmail failed; trying explicit SendGrid fallback")
        return _send_via_sendgrid(
            recipients=normalized,
            cc_recipients=normalized_cc,
            subject=subject,
            body=body,
            attachments=attachments,
        )
    if transport == "sendgrid":
        return _send_via_sendgrid(
            recipients=normalized,
            cc_recipients=normalized_cc,
            subject=subject,
            body=body,
            attachments=attachments,
        )

    logger.error("email_service: unsupported EMAIL_TRANSPORT=%s", settings.email_transport)
    return False


def send_alert(
    subject: str,
    body: str,
    level: str = "warning",
    recipients: Sequence[str] | None = None,
) -> bool:
    """
    Send an alert email to configured recipients or an explicit test recipient list.

    Args:
        subject: Short subject line (prefixed with level indicator automatically).
        body:    Plain-text message body.
        level:   "info" | "warning" | "critical"
        recipients: Optional explicit recipient list, used for safe tests.

    Returns True if all sends succeeded, False if any failed (non-blocking).
    """
    target_recipients = _normalize_recipients(recipients or _get_recipients())
    if not target_recipients:
        logger.warning("email_service: no recipients configured — skipping alert")
        return False

    level_prefix = LEVEL_SUBJECTS.get(level, "[ESIP]")
    full_subject = f"{level_prefix}: {subject}"
    now_str = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M %Z")
    full_body = f"{body}\n\n---\nSent: {now_str}\nESIP — Enterprise Support Intelligence Platform"

    return send_email(
        recipients=target_recipients,
        subject=full_subject,
        body=full_body,
    )


def send_feedback_notification(*, user_name: str, user_email: str, text: str) -> bool:
    """Notify the feedback owner about one newly submitted ESIP feedback entry."""
    display_name = user_name.strip() or user_email.strip() or "ESIP user"
    submitted_at = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M %Z")
    # Preserve the full feedback in ESIP; keep a notification email bounded in
    # case a user submits an unusually large message.
    preview = text.strip()
    if len(preview) > 12_000:
        preview = f"{preview[:12_000]}\n\n[Email preview truncated — open ESIP for the full feedback.]"

    return send_email(
        recipients=[_FEEDBACK_NOTIFICATION_RECIPIENT],
        subject=f"[ESIP] New feedback from {display_name}",
        body=(
            "A new feedback entry was submitted in ESIP.\n\n"
            f"From: {display_name}\n"
            f"Email: {user_email.strip() or 'Not available'}\n"
            f"Submitted: {submitted_at}\n\n"
            f"Feedback:\n{preview}\n\n"
            "Open ESIP → Admin → Feedbacks to review and reply."
        ),
    )
