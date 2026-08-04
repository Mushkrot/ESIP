"""Pure Universal Chat Answer Now control policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


ANSWER_NOW_ACTION = "answer_now"
ANSWER_NOW_PARTIAL_CAVEAT = (
    "Partial answer based on checks completed so far because you asked to answer now."
)


@dataclass(frozen=True)
class AnswerNowPolicyResult:
    visible_answer: str
    flags: dict[str, Any]


@dataclass(frozen=True)
class TurnControlEvaluation:
    status: str
    available: bool
    action: str | None = None
    reason: str = ""


def answer_now_candidate_policy(
    candidate_answer: str | None,
    *,
    answer_now_requested: bool,
    changed_finalization_policy: bool,
) -> AnswerNowPolicyResult:
    """Return user-visible partial-finalization policy for Answer Now.

    Answer Now is not a client abort: it waits for a safe candidate, then skips
    optional extra validation/repair work only when that changes finalization.
    """
    visible_answer = (candidate_answer or "").strip()
    flags: dict[str, Any] = {
        "answer_now_requested": bool(answer_now_requested),
        "changed_finalization_policy": bool(changed_finalization_policy),
        "partial_caveat_added": False,
        "wait_for_safe_candidate": False,
        "client_aborted": False,
        "action": "unchanged",
    }
    if not answer_now_requested:
        return AnswerNowPolicyResult(visible_answer=visible_answer, flags=flags)
    if not visible_answer:
        flags["wait_for_safe_candidate"] = True
        flags["action"] = "wait_for_safe_candidate"
        return AnswerNowPolicyResult(visible_answer="", flags=flags)
    if not changed_finalization_policy or ANSWER_NOW_PARTIAL_CAVEAT in visible_answer:
        return AnswerNowPolicyResult(visible_answer=visible_answer, flags=flags)

    flags["partial_caveat_added"] = True
    flags["action"] = "answer_now_partial_caveat"
    return AnswerNowPolicyResult(
        visible_answer=f"{visible_answer}\n\n{ANSWER_NOW_PARTIAL_CAVEAT}",
        flags=flags,
    )


def build_turn_control_payload(
    *,
    turn_id: str,
    conversation_id: str,
    owner_uid: str | None,
    owner_email: str | None,
    action: str = ANSWER_NOW_ACTION,
    now: datetime | None = None,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Build a short-lived, content-free turn control document."""
    current_time = _aware_utc(now)
    ttl = max(1, int(ttl_seconds or 1))
    return {
        "turn_id": str(turn_id or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "owner_uid": str(owner_uid or "").strip(),
        "owner_email": str(owner_email or "").strip().lower(),
        "action": str(action or "").strip(),
        "requested_at": current_time,
        "created_at": current_time,
        "expires_at": current_time + timedelta(seconds=ttl),
    }


def build_turn_registration_payload(
    *,
    turn_id: str,
    conversation_id: str,
    owner_uid: str | None,
    owner_email: str | None,
    now: datetime | None = None,
    ttl_seconds: int = 1800,
) -> dict[str, Any]:
    """Build the active-turn document the stream registers before polling."""
    current_time = _aware_utc(now)
    ttl = max(1, int(ttl_seconds or 1))
    return {
        "turn_id": str(turn_id or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "owner_uid": str(owner_uid or "").strip(),
        "owner_email": str(owner_email or "").strip().lower(),
        "status": "active",
        "answer_now_requested": False,
        "requested_action": None,
        "created_at": current_time,
        "updated_at": current_time,
        "expires_at": current_time + timedelta(seconds=ttl),
    }


def evaluate_active_turn_document(
    control: dict[str, Any] | None,
    *,
    owner_uid: str | None,
    owner_email: str | None,
    conversation_id: str | None,
    now: datetime | None = None,
) -> TurnControlEvaluation:
    """Validate an active turn document before reading or mutating control flags."""
    if not control:
        return TurnControlEvaluation(status="missing", available=False, reason="turn control was not found")

    current_time = _aware_utc(now)
    expires_at = _parse_datetime(control.get("expires_at"))
    if expires_at is None or current_time >= expires_at:
        return TurnControlEvaluation(status="expired", available=False, reason="turn control expired")

    if str(control.get("status") or "active").strip() == "completed":
        return TurnControlEvaluation(status="completed", available=False, reason="turn already completed")

    expected_uid = str(owner_uid or "").strip()
    expected_email = str(owner_email or "").strip().lower()
    actual_uid = str(control.get("owner_uid") or "").strip()
    actual_email = str(control.get("owner_email") or "").strip().lower()
    if (expected_uid and actual_uid != expected_uid) or (expected_email and actual_email != expected_email):
        return TurnControlEvaluation(status="forbidden", available=False, reason="turn belongs to another user")

    expected_conversation = str(conversation_id or "").strip()
    actual_conversation = str(control.get("conversation_id") or "").strip()
    if expected_conversation and actual_conversation and expected_conversation != actual_conversation:
        return TurnControlEvaluation(
            status="conversation_mismatch",
            available=False,
            reason="turn belongs to another conversation",
        )

    return TurnControlEvaluation(status="active", available=True)


def evaluate_turn_control_document(
    control: dict[str, Any] | None,
    *,
    owner_uid: str | None,
    owner_email: str | None,
    conversation_id: str | None,
    now: datetime | None = None,
) -> TurnControlEvaluation:
    """Validate a turn control document without touching Firestore."""
    base = evaluate_active_turn_document(
        control,
        owner_uid=owner_uid,
        owner_email=owner_email,
        conversation_id=conversation_id,
        now=now,
    )
    if not base.available:
        return base

    action = str(control.get("action") or "").strip()
    if action != ANSWER_NOW_ACTION:
        return TurnControlEvaluation(status="unsupported_action", available=False, action=action)

    return TurnControlEvaluation(status="active", available=True, action=action)


def _aware_utc(value: datetime | None) -> datetime:
    current_time = value or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        return current_time.replace(tzinfo=timezone.utc)
    return current_time.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    if isinstance(value, str):
        try:
            return _aware_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None
