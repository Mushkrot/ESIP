"""
Auth endpoints.
GET /api/auth/me — validate token and return user info + ensure profile exists.
"""

from fastapi import APIRouter

from app.config import settings
from app.dependencies import CurrentUser
from app.models.profile import AuthUserResponse
from app.services import firestore

router = APIRouter()


@router.get("/me", response_model=AuthUserResponse)
def get_current_user_info(user: CurrentUser):
    """
    Validate the ID token (done by the dependency) and return user claims.
    Also ensures a Firestore profile record exists for this user (created on first call).
    """
    uid: str = user["uid"]
    email: str = user.get("email", "")
    name: str = firestore.clean_auth_display_name(user.get("name"), email)
    picture: str | None = user.get("picture")

    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        role = str(user.get("effective_role") or user.get("role") or "super_admin")
        return AuthUserResponse(
            uid=uid,
            email=email,
            name=name,
            picture=picture,
            is_admin=True,
            role=role,
            actual_role=str(user.get("actual_role") or role),
            effective_role=role,
            role_preview=bool(user.get("role_preview")),
            can_preview_roles=True,
        )

    # Ensure profile exists (no-op if already created)
    firestore.get_or_create_profile(uid, email, name, picture)
    firestore.sync_user_identity_from_auth(
        email=email,
        name=name,
        picture=picture,
        source="firebase",
        uid=uid,
    )

    role = str(user.get("effective_role") or user.get("role") or firestore.get_user_role(email))
    return AuthUserResponse(
        uid=uid,
        email=email,
        name=name,
        picture=picture,
        is_admin=role in (firestore.ROLE_ADMIN, firestore.ROLE_SUPER_ADMIN),
        role=role,
        actual_role=str(user.get("actual_role") or role),
        effective_role=role,
        role_preview=bool(user.get("role_preview")),
        can_preview_roles=bool(user.get("can_preview_roles")),
    )
