"""
Feedback endpoints — used during the beta testing period.

POST   /api/feedback          — submit a new feedback entry (authenticated)
GET    /api/feedback          — get the current user's own feedback entries
PUT    /api/feedback/{id}     — update one of the current user's entries
GET    /api/feedback/inbox    — unread feedback-reply notifications for current user
POST   /api/feedback/inbox/read — mark inbox notifications as read
GET    /api/feedback/all      — super admin: all feedback entries
POST   /api/feedback/{uid}/{entry_id}/reply — super admin: reply to a user's feedback
"""

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator

from app.config import settings
from app.dependencies import CurrentUser, reject_role_preview_write, require_super_admin
from app.services.email_service import send_feedback_notification
from app.services import firestore, reference_runtime

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class FeedbackSubmitRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Feedback text cannot be empty.")
        return v


class FeedbackUpdateRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Feedback text cannot be empty.")
        return v


class FeedbackReplyRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reply text cannot be empty.")
        return v


class InboxReadRequest(BaseModel):
    message_ids: list[str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_timestamp_fields(entry: dict, fields: tuple[str, ...]) -> dict:
    """Convert selected Firestore Timestamp fields to ISO-8601 strings."""
    result = dict(entry)
    for field in fields:
        val = result.get(field)
        if hasattr(val, "isoformat"):
            result[field] = val.isoformat()
    return result


def _serialize_reply(reply: dict) -> dict:
    return _serialize_timestamp_fields(reply, ("created_at",))


def _serialize_inbox_message(msg: dict) -> dict:
    return _serialize_timestamp_fields(msg, ("created_at", "read_at"))


def _serialize_feedback_entry(entry: dict, replies: list[dict] | None = None) -> dict:
    out = _serialize_timestamp_fields(entry, ("created_at", "updated_at"))
    out["replies"] = [_serialize_reply(r) for r in (replies or [])]
    return out


def _require_super_admin(user: dict) -> None:
    require_super_admin(user)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/all")
def get_all_feedback(user: CurrentUser):
    """Return all feedback from all users. Super Admin only."""
    _require_super_admin(user)
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.all_feedback()
    entries = firestore.get_all_feedback_entries()
    out = []
    for entry in entries:
        replies = firestore.get_feedback_replies(entry["uid"], entry["id"])
        out.append(_serialize_feedback_entry(entry, replies))
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
def submit_feedback(req: FeedbackSubmitRequest, user: CurrentUser):
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.add_feedback(
            uid=user["uid"],
            email=user.get("email", ""),
            name=user.get("name", ""),
            text=req.text,
        )
    entry = firestore.add_feedback_entry(
        uid=user["uid"],
        email=user.get("email", ""),
        name=user.get("name", ""),
        text=req.text,
    )
    try:
        notification_sent = send_feedback_notification(
            user_name=user.get("name", ""),
            user_email=user.get("email", ""),
            text=entry["text"],
        )
        if not notification_sent:
            logger.warning("feedback: notification was not sent for entry %s", entry["id"])
    except Exception:
        # The feedback entry is already durable. A mail transport problem must
        # never make a user's submission appear to have failed.
        logger.exception("feedback: notification failed for entry %s", entry["id"])
    return _serialize_feedback_entry(entry, [])


@router.get("/inbox")
def get_my_feedback_inbox(user: CurrentUser):
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.feedback_inbox()
    messages = firestore.get_feedback_inbox(user["uid"], unread_only=True)
    return [_serialize_inbox_message(m) for m in messages]


@router.post("/inbox/read")
def mark_my_feedback_inbox_read(req: InboxReadRequest, user: CurrentUser):
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return {"updated": 0}
    updated = firestore.mark_feedback_inbox_read(user["uid"], req.message_ids)
    return {"updated": updated}


@router.get("")
def get_my_feedback(user: CurrentUser):
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.list_feedback(user["uid"])
    entries = firestore.get_feedback_entries(user["uid"])
    out = []
    for entry in entries:
        replies = firestore.get_feedback_replies(user["uid"], entry["id"])
        out.append(_serialize_feedback_entry(entry, replies))
    return out


@router.put("/{entry_id}")
def update_feedback(entry_id: str, req: FeedbackUpdateRequest, user: CurrentUser):
    reject_role_preview_write(user)
    updated = firestore.update_feedback_entry(user["uid"], entry_id, req.text)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feedback entry not found.",
        )
    replies = firestore.get_feedback_replies(user["uid"], entry_id)
    return _serialize_feedback_entry(updated, replies)


@router.post("/{uid}/{entry_id}/reply", status_code=status.HTTP_201_CREATED)
def reply_to_feedback(uid: str, entry_id: str, req: FeedbackReplyRequest, user: CurrentUser):
    """Super Admin reply to a user's feedback entry + create unread inbox notification for the user."""
    _require_super_admin(user)
    reject_role_preview_write(user)

    entry = firestore.get_feedback_entry(uid, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feedback entry not found.",
        )

    reply = firestore.add_feedback_reply(
        uid=uid,
        entry_id=entry_id,
        text=req.text,
        author_email=user.get("email", ""),
        author_name=user.get("name", "") or user.get("email", "Admin"),
        is_admin_reply=True,
    )
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feedback entry not found.",
        )

    preview = req.text.strip().replace("\n", " ")
    firestore.add_feedback_inbox_message(
        uid=uid,
        entry_id=entry_id,
        reply_id=reply["id"],
        preview=preview,
    )
    return _serialize_reply(reply)
