"""User-facing product announcements."""

from fastapi import APIRouter, HTTPException, Query, status

from app.config import settings
from app.dependencies import CurrentUser, get_effective_role, reject_role_preview_write
from app.services import announcements, reference_runtime

router = APIRouter()


@router.get("/active")
def active_announcements(
    user: CurrentUser,
    surface: str = Query(default="web"),
):
    """Return active, not-dismissed announcements for the signed-in user."""
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return [item for item in reference_runtime.announcement_items() if not item.get("read_state")]
    email = user.get("email") or ""
    role = get_effective_role(user)
    return announcements.list_active_for_user(
        email=email,
        role=role,
        surface=surface,
        include_dismissed=False,
    )


@router.get("/history")
def announcement_history(
    user: CurrentUser,
    surface: str = Query(default="web"),
):
    """Return published announcement history for the signed-in user."""
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.announcement_items()
    email = user.get("email") or ""
    role = get_effective_role(user)
    return announcements.list_history_for_user(
        email=email,
        role=role,
        surface=surface,
    )


@router.post("/{announcement_id}/dismiss")
def dismiss_announcement(announcement_id: str, user: CurrentUser):
    """Mark one announcement dismissed for the signed-in user."""
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        result = reference_runtime.dismiss_announcement(announcement_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
        return result
    email = user.get("email") or ""
    role = get_effective_role(user)
    result = announcements.dismiss_announcement(
        announcement_id=announcement_id,
        email=email,
        role=role,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return result
