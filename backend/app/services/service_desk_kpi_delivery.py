"""Service Desk KPI email delivery configuration and orchestration."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.api_core.exceptions import AlreadyExists, Conflict

from app.services import email_service, service_desk_kpi_snapshot
from app.reference_data.fixtures import PLATFORM_ADMIN_EMAIL, SUPPORT_ADMIN_EMAIL
from app.services.email_service import EmailAttachment

logger = logging.getLogger(__name__)

CONFIG_DOC_PATH = "system/service_desk_kpi_delivery_config"
RUNS_COLLECTION = "service_desk_kpi_delivery_runs"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_SLOT_TIME = "10:30"
SAFE_TEST_RECIPIENTS = {
    SUPPORT_ADMIN_EMAIL,
    PLATFORM_ADMIN_EMAIL,
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SAFE_ID_RE = re.compile(r"[^a-z0-9_-]+")


class DeliveryValidationError(ValueError):
    """Raised when KPI delivery config or request data is invalid."""


class DeliverySendError(RuntimeError):
    """Raised when the configured email transport does not send the report."""


@dataclass(frozen=True)
class DeliverySlot:
    id: str
    time: str
    enabled: bool = True
    label: str | None = None


@dataclass(frozen=True)
class DeliveryConfig:
    enabled: bool = False
    timezone: str = DEFAULT_TIMEZONE
    recipients: tuple[str, ...] = ()
    cc_recipients: tuple[str, ...] = ()
    slots: tuple[DeliverySlot, ...] = field(
        default_factory=lambda: (DeliverySlot(id="slot-1030", time=DEFAULT_SLOT_TIME, label="Daily morning"),)
    )
    last_delivery: dict[str, Any] | None = None
    last_test: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeliveryRunClaim:
    claimed: bool
    run_id: str
    status: str
    existing: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    report_date: str
    slot_id: str | None = None
    slot_time: str | None = None
    recipient_count: int = 0
    cc_recipient_count: int = 0
    gcs_uri: str | None = None
    run_id: str | None = None
    snapshot_status: str | None = None
    message: str = ""


def normalize_email(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise DeliveryValidationError(f"Invalid email: {email}")
    return normalized


def normalize_recipients(recipients: Sequence[str] | None, *, allow_empty: bool = True) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in recipients or ():
        normalized = normalize_email(str(raw))
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    if not out and not allow_empty:
        raise DeliveryValidationError("At least one recipient is required")
    return tuple(out)


def validate_timezone(timezone_name: str | None) -> str:
    value = str(timezone_name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise DeliveryValidationError(f"Invalid timezone: {value}") from exc
    return value


def normalize_slot_time(value: str) -> str:
    raw = str(value or "").strip()
    match = _TIME_RE.match(raw)
    if not match:
        raise DeliveryValidationError("Slot time must use HH:MM 24-hour format")
    minute = int(match.group(2))
    if minute % 15 != 0:
        raise DeliveryValidationError("Slot time must be on a 15-minute increment")
    return raw


def stable_slot_id(slot_time: str, label: str | None = None) -> str:
    base = _SAFE_ID_RE.sub("-", str(label or "").strip().lower()).strip("-")
    suffix = slot_time.replace(":", "")
    return f"{base}-{suffix}" if base else f"slot-{suffix}"


def normalize_slot(slot: DeliverySlot | dict[str, Any]) -> DeliverySlot:
    data = asdict(slot) if isinstance(slot, DeliverySlot) else dict(slot or {})
    slot_time = normalize_slot_time(str(data.get("time") or ""))
    label = str(data.get("label") or "").strip() or None
    slot_id = str(data.get("id") or "").strip().lower() or stable_slot_id(slot_time, label)
    slot_id = _SAFE_ID_RE.sub("-", slot_id).strip("-")
    if not slot_id:
        slot_id = stable_slot_id(slot_time, label)
    return DeliverySlot(
        id=slot_id,
        time=slot_time,
        enabled=bool(data.get("enabled", True)),
        label=label,
    )


def normalize_slots(slots: Sequence[DeliverySlot | dict[str, Any]] | None) -> tuple[DeliverySlot, ...]:
    source: Iterable[DeliverySlot | dict[str, Any]]
    if slots is None:
        source = (DeliverySlot(id="slot-1030", time=DEFAULT_SLOT_TIME),)
    else:
        source = slots
    normalized = tuple(normalize_slot(slot) for slot in source)
    seen: set[str] = set()
    for slot in normalized:
        if slot.id in seen:
            raise DeliveryValidationError(f"Duplicate slot id: {slot.id}")
        seen.add(slot.id)
    return normalized


def normalize_config(data: DeliveryConfig | dict[str, Any] | None = None) -> DeliveryConfig:
    if isinstance(data, DeliveryConfig):
        payload = asdict(data)
    else:
        payload = dict(data or {})
    slots_payload = payload.get("slots") if "slots" in payload else None
    return DeliveryConfig(
        enabled=bool(payload.get("enabled", False)),
        timezone=validate_timezone(payload.get("timezone") or DEFAULT_TIMEZONE),
        recipients=normalize_recipients(payload.get("recipients") or (), allow_empty=True),
        cc_recipients=normalize_recipients(payload.get("cc_recipients") or (), allow_empty=True),
        slots=normalize_slots(slots_payload),
        last_delivery=payload.get("last_delivery") or None,
        last_test=payload.get("last_test") or None,
    )


def config_to_dict(config: DeliveryConfig) -> dict[str, Any]:
    normalized = normalize_config(config)
    return {
        "enabled": normalized.enabled,
        "timezone": normalized.timezone,
        "recipients": list(normalized.recipients),
        "cc_recipients": list(normalized.cc_recipients),
        "slots": [asdict(slot) for slot in normalized.slots],
        "last_delivery": normalized.last_delivery,
        "last_test": normalized.last_test,
    }


def get_delivery_config() -> DeliveryConfig:
    from app.services import firestore  # noqa: PLC0415

    doc = firestore.get_db().document(CONFIG_DOC_PATH).get()
    data = doc.to_dict() if getattr(doc, "exists", False) else {}
    return normalize_config(data)


def save_delivery_config(config: DeliveryConfig | dict[str, Any]) -> DeliveryConfig:
    from app.services import firestore  # noqa: PLC0415

    normalized = normalize_config(config)
    firestore.get_db().document(CONFIG_DOC_PATH).set(config_to_dict(normalized), merge=True)
    return normalized


def _localize_now(now: datetime | None, timezone_name: str) -> datetime:
    tz = ZoneInfo(validate_timezone(timezone_name))
    value = now or datetime.now(tz)
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def current_dispatch_time(now: datetime | None, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    local = _localize_now(now, timezone_name)
    minute_bucket = (local.minute // 15) * 15
    return f"{local.hour:02d}:{minute_bucket:02d}"


def due_slots(config: DeliveryConfig | dict[str, Any], now: datetime | None = None) -> list[DeliverySlot]:
    normalized = normalize_config(config)
    if not normalized.enabled:
        return []
    due_time = current_dispatch_time(now, normalized.timezone)
    return [slot for slot in normalized.slots if slot.enabled and slot.time == due_time]


def run_id_for(report_date: str, slot_id: str) -> str:
    safe_slot = _SAFE_ID_RE.sub("-", str(slot_id or "").strip().lower()).strip("-") or "slot"
    return f"{service_desk_kpi_snapshot.normalize_report_date(report_date)}_{safe_slot}"


def _get_run_ref(db: Any, run_id: str) -> Any:
    return db.collection(RUNS_COLLECTION).document(run_id)


def claim_delivery_run(
    *,
    report_date: str,
    slot: DeliverySlot,
    now: datetime | None = None,
    db: Any | None = None,
) -> DeliveryRunClaim:
    """Atomically claim one report-date/slot pair before sending."""
    from app.services import firestore  # noqa: PLC0415

    report_day = service_desk_kpi_snapshot.normalize_report_date(report_date)
    run_id = run_id_for(report_day, slot.id)
    database = db or firestore.get_db()
    run_ref = _get_run_ref(database, run_id)
    local_now = _localize_now(now, DEFAULT_TIMEZONE).isoformat()
    payload = {
        "run_id": run_id,
        "report_date": report_day,
        "slot_id": slot.id,
        "slot_time": slot.time,
        "status": "claimed",
        "claimed_at": local_now,
    }
    try:
        # Firestore create uses an exists=false precondition, so this is the
        # single-document atomic claim that prevents duplicate sends.
        run_ref.create(payload)
        return DeliveryRunClaim(claimed=True, run_id=run_id, status="claimed")
    except (AlreadyExists, Conflict):
        snap = run_ref.get()
        existing = snap.to_dict() if getattr(snap, "exists", False) else None
        return DeliveryRunClaim(
            claimed=False,
            run_id=run_id,
            status=str((existing or {}).get("status") or "already_claimed"),
            existing=existing,
        )


def mark_delivery_run_sent(
    *,
    run_id: str,
    outcome: DeliveryOutcome,
    now: datetime | None = None,
    db: Any | None = None,
) -> None:
    from app.services import firestore  # noqa: PLC0415

    database = db or firestore.get_db()
    _get_run_ref(database, run_id).set(
        {
            "status": "sent",
            "sent_at": _localize_now(now, DEFAULT_TIMEZONE).isoformat(),
            "recipient_count": outcome.recipient_count,
            "cc_recipient_count": outcome.cc_recipient_count,
            "gcs_uri": outcome.gcs_uri,
            "snapshot_status": outcome.snapshot_status,
        },
        merge=True,
    )


def mark_delivery_run_failed(
    *,
    run_id: str,
    error: Exception,
    now: datetime | None = None,
    db: Any | None = None,
) -> None:
    from app.services import firestore  # noqa: PLC0415

    database = db or firestore.get_db()
    _get_run_ref(database, run_id).set(
        {
            "status": "failed",
            "failed_at": _localize_now(now, DEFAULT_TIMEZONE).isoformat(),
            "error_type": type(error).__name__,
            "error_message": str(error)[:500],
        },
        merge=True,
    )


def _format_report_date_for_email(report_date: str) -> str:
    normalized = service_desk_kpi_snapshot.normalize_report_date(report_date)
    parsed = datetime.strptime(normalized, "%Y-%m-%d")
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _delivery_subject(report_date: str, *, test: bool = False) -> str:
    return f"Daily KPI Report {_format_report_date_for_email(report_date)}"


def _delivery_body(report_date: str, gcs_uri: str, *, test: bool = False) -> str:
    return (
        "Hi Team,\n\n"
        "Please see the KPI report attached."
    )


def _snapshot_attachment(report_date: str) -> tuple[service_desk_kpi_snapshot.SnapshotResult, EmailAttachment]:
    result = service_desk_kpi_snapshot.write_daily_snapshot(report_date=report_date)
    download = service_desk_kpi_snapshot.download_snapshot(report_date)
    return result, EmailAttachment(
        filename=download.filename,
        content=download.content,
        media_type=download.media_type,
    )


def deliver_slot(
    config: DeliveryConfig | dict[str, Any],
    slot: DeliverySlot,
    *,
    report_date: str | None = None,
    now: datetime | None = None,
    db: Any | None = None,
) -> DeliveryOutcome:
    normalized = normalize_config(config)
    report_day = service_desk_kpi_snapshot.normalize_report_date(report_date)
    if not normalized.enabled:
        return DeliveryOutcome(status="skipped", report_date=report_day, slot_id=slot.id, slot_time=slot.time, message="disabled")
    if not normalized.recipients:
        return DeliveryOutcome(status="skipped", report_date=report_day, slot_id=slot.id, slot_time=slot.time, message="no recipients")

    claim = claim_delivery_run(report_date=report_day, slot=slot, now=now, db=db)
    if not claim.claimed:
        return DeliveryOutcome(
            status="skipped",
            report_date=report_day,
            slot_id=slot.id,
            slot_time=slot.time,
            run_id=claim.run_id,
            message="already sent" if claim.status == "sent" else "already claimed",
        )

    try:
        snapshot_result, attachment = _snapshot_attachment(report_day)
        sent = email_service.send_email(
            recipients=normalized.recipients,
            cc_recipients=normalized.cc_recipients,
            subject=_delivery_subject(report_day),
            body=_delivery_body(report_day, snapshot_result.gcs_uri),
            attachments=[attachment],
        )
        if not sent:
            raise DeliverySendError("Email transport returned false")
        outcome = DeliveryOutcome(
            status="sent",
            report_date=report_day,
            slot_id=slot.id,
            slot_time=slot.time,
            recipient_count=len(normalized.recipients),
            cc_recipient_count=len(normalized.cc_recipients),
            gcs_uri=snapshot_result.gcs_uri,
            run_id=claim.run_id,
            snapshot_status=snapshot_result.status,
            message="sent",
        )
        try:
            mark_delivery_run_sent(run_id=claim.run_id, outcome=outcome, now=now, db=db)
        except Exception as exc:
            logger.warning("service_desk_kpi_delivery: email sent but run status update failed: %s", exc)
        return outcome
    except Exception as exc:
        mark_delivery_run_failed(run_id=claim.run_id, error=exc, now=now, db=db)
        raise


def deliver_due(
    *,
    now: datetime | None = None,
    report_date: str | None = None,
    db: Any | None = None,
) -> list[DeliveryOutcome]:
    config = get_delivery_config()
    slots = due_slots(config, now)
    if not slots:
        return []
    return [deliver_slot(config, slot, report_date=report_date, now=now, db=db) for slot in slots]


def send_test_delivery(
    *,
    recipient: str,
    report_date: str | None = None,
    now: datetime | None = None,
) -> DeliveryOutcome:
    target = normalize_email(recipient)
    if target not in SAFE_TEST_RECIPIENTS:
        raise DeliveryValidationError(
            f"KPI test emails can only be sent to {SUPPORT_ADMIN_EMAIL} or {PLATFORM_ADMIN_EMAIL}"
        )
    report_day = service_desk_kpi_snapshot.normalize_report_date(report_date)
    snapshot_result, attachment = _snapshot_attachment(report_day)
    sent = email_service.send_email(
        recipients=[target],
        subject=_delivery_subject(report_day, test=True),
        body=_delivery_body(report_day, snapshot_result.gcs_uri, test=True),
        attachments=[attachment],
    )
    if not sent:
        raise DeliverySendError("Email transport returned false")
    return DeliveryOutcome(
        status="sent",
        report_date=report_day,
        recipient_count=1,
        gcs_uri=snapshot_result.gcs_uri,
        snapshot_status=snapshot_result.status,
        message=f"test sent to {target}",
    )
