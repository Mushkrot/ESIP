"""
FastAPI shared dependencies.

Auth flow:
  1. Frontend sends  Authorization: Bearer {Firebase ID token}
  2. verify_firebase_token() validates the token via Firebase Admin SDK
  3. Domain check: must match the configured tenant identity domain
  4. Allowlist check: email must exist in Firestore users/{email} with active=True
  5. Returns the decoded token claims dict (used by route handlers)
"""

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.services import google_auth, firestore, reference_runtime

logger = logging.getLogger(__name__)

_bearer = HTTPBearer()
PREVIEW_ROLE_HEADER = "X-ESIP-Preview-Role"


def _clean_role(value: str | None) -> str | None:
    role = (value or "").strip()
    return role or None


def attach_role_context(claims: dict, preview_role: str | None = None) -> dict:
    """
    Attach actual/effective role metadata to Firebase-authenticated claims.

    Role preview does not bypass authentication: only a real Firestore
    super_admin may request an effective admin/user role for testing.
    """
    out = dict(claims)
    email = (out.get("email") or "").strip().lower()
    actual_role = (
        str(out.get("actual_role") or out.get("role") or firestore.ROLE_USER)
        if out.get("reference_runtime")
        else firestore.get_user_role(email)
    )
    requested_role = _clean_role(preview_role)

    effective_role = actual_role
    preview_mode = False
    if requested_role is not None:
        if requested_role not in firestore.VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid role preview. Must be one of: {sorted(firestore.VALID_ROLES)}",
            )
        if actual_role != firestore.ROLE_SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Role preview is available only to Super Admin.",
            )
        effective_role = requested_role
        preview_mode = requested_role != actual_role

    out["email"] = email
    out["actual_role"] = actual_role
    out["effective_role"] = effective_role
    out["role"] = effective_role
    out["role_preview"] = preview_mode
    out["can_preview_roles"] = actual_role == firestore.ROLE_SUPER_ADMIN
    return out


def get_actual_role(user: dict) -> str:
    return str(user.get("actual_role") or firestore.get_user_role(user.get("email", "")))


def get_effective_role(user: dict) -> str:
    return str(user.get("effective_role") or user.get("role") or get_actual_role(user))


def is_effective_admin(user: dict) -> bool:
    return get_effective_role(user) in (firestore.ROLE_ADMIN, firestore.ROLE_SUPER_ADMIN)


def is_effective_super_admin(user: dict) -> bool:
    return get_effective_role(user) == firestore.ROLE_SUPER_ADMIN


def is_role_preview(user: dict | None) -> bool:
    return bool(user and user.get("role_preview"))


def require_admin(user: dict) -> None:
    if not is_effective_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")


def require_super_admin(user: dict) -> None:
    if not is_effective_super_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super Admin only")


def reject_role_preview_write(user: dict | None) -> None:
    if is_role_preview(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Role preview is read-only for this action.",
        )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    x_esip_preview_role: Annotated[str | None, Header(alias=PREVIEW_ROLE_HEADER)] = None,
) -> dict:
    """
    Validate a Firebase ID token and enforce the allowlist.
    Raises HTTP 401 / 403 / 503 on any failure.
    Returns decoded token claims dict (keys: uid, email, name, picture, ...).
    """
    token = credentials.credentials

    if settings.runtime_profile == "reference" and token == reference_runtime.REFERENCE_SESSION_TOKEN:
        return attach_role_context(reference_runtime.claims(), x_esip_preview_role)

    # 1. Validate the Firebase ID token
    try:
        claims = google_auth.verify_id_token(token)
    except Exception as exc:
        logger.warning("Token validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Email claims may be mixed-case; allowlist keys are normalized in Firestore helpers.
    email: str = (claims.get("email") or "").strip().lower()

    # 2. Domain check — configured tenant identity domains.
    if not any(email.endswith(f"@{d}") for d in settings.allowed_email_domains_list):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to approved Northstar accounts.",
        )

    # 3. Maintenance (before generic allowlist so message is correct)
    if firestore.is_maintenance_blocked(email):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System is currently in maintenance mode.",
        )

    # 4. Firestore allowlist (second layer — domain alone is insufficient)
    if not firestore.is_user_allowed(email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has not been provisioned for ESIP. Contact IT.",
        )

    return attach_role_context(claims, x_esip_preview_role)


# Convenient type alias for route function signatures
CurrentUser = Annotated[dict, Depends(get_current_user)]


def get_any_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    x_esip_preview_role: Annotated[str | None, Header(alias=PREVIEW_ROLE_HEADER)] = None,
) -> dict:
    """
    Accept either a Firebase ID token or a ZAF session token.
    Used by endpoints that must be accessible from both the ESIP web app and the ZAF sidebar.
    Tries Firebase first; falls back to ZAF session lookup.
    """
    token = credentials.credentials

    if settings.runtime_profile == "reference" and token == reference_runtime.REFERENCE_SESSION_TOKEN:
        return attach_role_context(reference_runtime.claims(), x_esip_preview_role)

    # Try Firebase token first
    try:
        claims = google_auth.verify_id_token(token)
    except Exception:
        claims = None

    if claims is not None:
        email: str = (claims.get("email") or "").strip().lower()
        if not any(email.endswith(f"@{d}") for d in settings.allowed_email_domains_list):
            # Valid Firebase token but email domain not in allowlist — try ZAF with same string
            pass
        else:
            if firestore.is_maintenance_blocked(email):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="System is currently in maintenance mode.",
                )
            if firestore.is_user_allowed(email):
                return attach_role_context(claims, x_esip_preview_role)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has not been provisioned for ESIP. Contact IT.",
            )

    # Try ZAF session token
    session = firestore.validate_zaf_session(token)
    if session is not None:
        if firestore.is_maintenance_blocked(session.get("email", "")):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="System is currently in maintenance mode.",
            )
        return session

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


AnyUser = Annotated[dict, Depends(get_any_user)]
