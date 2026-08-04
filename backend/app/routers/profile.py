"""
User profile endpoints.

GET    /api/profile                — get current user profile
PATCH  /api/profile                — update theme, search_mode, or sidebar_sections
POST   /api/profile/tabs           — add ticket to open tabs (max 8)
DELETE /api/profile/tabs/{id}      — remove ticket from open tabs
POST   /api/profile/search-history — prepend a search to history (max 10)
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

from app.dependencies import CurrentUser, reject_role_preview_write
from app.models.profile import (
    AddSearchHistoryRequest,
    AddTabRequest,
    MyUnsolvedTicketsResponse,
    ProfileUpdateRequest,
    UserProfile,
)
from app.config import settings
from app.services import firestore, my_unsolved_tickets, reference_runtime

router = APIRouter()


@router.get("", response_model=UserProfile)
def get_profile(user: CurrentUser):
    uid = user["uid"]
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.get_profile(
            uid=uid,
            email=user.get("email", ""),
            name=user.get("name", ""),
            picture=user.get("picture"),
        )
    profile = firestore.get_or_create_profile(
        uid=uid,
        email=user.get("email", ""),
        name=firestore.clean_auth_display_name(user.get("name"), user.get("email", "")),
        picture=user.get("picture"),
    )
    return profile


@router.get("/my-unsolved-tickets", response_model=MyUnsolvedTicketsResponse)
def get_my_unsolved_tickets(
    background_tasks: BackgroundTasks,
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=50),
):
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.my_unsolved_tickets(limit)

    def enqueue_refresh(uid: str, email: str, refresh_limit: int) -> None:
        background_tasks.add_task(
            my_unsolved_tickets.refresh_my_unsolved_tickets_cache,
            uid,
            email,
            refresh_limit,
        )

    return my_unsolved_tickets.get_my_unsolved_tickets(
        uid=user["uid"],
        email=user.get("email", ""),
        limit=limit,
        enqueue_refresh=enqueue_refresh,
    )


@router.post("/my-unsolved-tickets/refresh", response_model=MyUnsolvedTicketsResponse)
def refresh_my_unsolved_tickets(
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=50),
):
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return reference_runtime.my_unsolved_tickets(limit)
    return my_unsolved_tickets.refresh_my_unsolved_tickets(
        uid=user["uid"],
        email=user.get("email", ""),
        limit=limit,
    )


@router.patch("", response_model=dict)
def update_profile(req: ProfileUpdateRequest, user: CurrentUser):
    reject_role_preview_write(user)
    updates = req.model_dump(exclude_none=True)
    if not updates:
        return {"updated": []}
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        reference_runtime.update_profile(user["uid"], updates)
    else:
        firestore.update_profile(user["uid"], updates)
    return {"updated": list(updates.keys())}


@router.post("/tabs", response_model=dict)
def add_tab(req: AddTabRequest, user: CurrentUser):
    reject_role_preview_write(user)
    tab_info = {
        "id": req.ticket_id,
        "subject": req.subject,
        "status": req.status,
        "priority": req.priority,
    }
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        tabs, evicted_id = reference_runtime.add_open_tab(user["uid"], tab_info)
    else:
        tabs, evicted_id = firestore.add_open_tab(user["uid"], tab_info)
    return {"tabs": tabs, "evicted_id": evicted_id}


@router.delete("/tabs/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tab(ticket_id: int, user: CurrentUser):
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        reference_runtime.remove_open_tab(user["uid"], ticket_id)
    else:
        firestore.remove_open_tab(user["uid"], ticket_id)


@router.post("/search-history", status_code=status.HTTP_204_NO_CONTENT)
def add_search_history(req: AddSearchHistoryRequest, user: CurrentUser):
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        reference_runtime.add_search_history(user["uid"], req.query, req.type)
    else:
        firestore.add_search_history(user["uid"], req.query, req.type)
