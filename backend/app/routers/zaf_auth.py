"""
Zendesk App Framework (ZAF) authentication.

POST /api/zaf/auth
  Accepts {email, name, role} from the ZAF app (obtained via client.get('currentUser')).
  Checks email domain + Firestore allowlist, issues a short-lived session token.
  The session token is used as Bearer for all subsequent ZAF API requests.

Security model: the app is private (not on Zendesk marketplace), installed only on
support.example.com. Only authenticated Zendesk agents can load the iframe and call this
endpoint. Domain + allowlist checks provide the authorization layer.
"""

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.reference_data.fixtures import USER_BY_EMAIL
from app.services import firestore, reference_runtime

logger = logging.getLogger(__name__)
router = APIRouter()


class ZafAuthRequest(BaseModel):
    email: str
    name: str
    role: str


class ZafAuthResponse(BaseModel):
    session_token: str
    email: str
    name: str
    role: str


def _log_zaf_auth_denied(email: str, role: str, reason: str) -> None:
    safe_email = email or "<empty>"
    safe_role = role or "<empty>"
    logger.warning("ZAF auth denied for %s (role=%s, reason=%s)", safe_email, safe_role, reason)


@router.post("/auth", response_model=ZafAuthResponse)
def zaf_auth(body: ZafAuthRequest) -> ZafAuthResponse:
    """
    Exchange ZAF user identity for a Firestore session token.
    Called once by the ZAF app on iframe load.
    """
    submitted_email = body.email.strip().lower()
    role = body.role or "agent"

    if not submitted_email:
        _log_zaf_auth_denied(submitted_email, role, "empty_email")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is required.",
        )

    if settings.runtime_profile == "reference" and role == "reference":
        fixture = USER_BY_EMAIL.get(submitted_email)
        if fixture is None:
            _log_zaf_auth_denied(submitted_email, role, "not_reference_identity")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has not been provisioned for ESIP.",
            )
        return ZafAuthResponse(
            session_token=reference_runtime.REFERENCE_SESSION_TOKEN,
            email=fixture.email,
            name=fixture.name,
            role=fixture.role,
        )

    email = firestore.resolve_zaf_identity_email(submitted_email)
    if email is None:
        _log_zaf_auth_denied(submitted_email, role, "unresolved_legacy_alias")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to approved Northstar accounts.",
        )
    name = firestore.clean_auth_display_name(body.name, email)

    # Domain check
    if not any(email.endswith(f"@{d}") for d in settings.allowed_email_domains_list):
        _log_zaf_auth_denied(submitted_email, role, "bad_domain")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to approved Northstar accounts.",
        )

    # Maintenance (provisioned non-admins cannot start a new ZAF session)
    if firestore.is_maintenance_blocked(email):
        _log_zaf_auth_denied(email, role, "maintenance_blocked")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System is currently in maintenance mode.",
        )

    # Allowlist check
    if not firestore.is_user_allowed(email):
        _log_zaf_auth_denied(email, role, "not_allowlisted")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has not been provisioned for ESIP. Contact IT.",
        )

    firestore.sync_user_identity_from_auth(
        email=email,
        name=name,
        source="zaf",
    )
    session_token = firestore.create_zaf_session(email, name, role)
    logger.info("ZAF session created for %s (role=%s)", email, role)

    return ZafAuthResponse(session_token=session_token, email=email, name=name, role=role)
