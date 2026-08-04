"""Google / Firebase ID token validation."""

import firebase_admin
from firebase_admin import auth, credentials

from app.config import settings

def _init_firebase() -> firebase_admin.App:
    """Return the shared Firebase app, initializing it only when auth is used."""
    try:
        return firebase_admin.get_app()
    except ValueError:
        cred_path = str(settings.google_application_credentials or "").strip()
        options = {"projectId": settings.firebase_project_id}
        if cred_path:
            return firebase_admin.initialize_app(credentials.Certificate(cred_path), options)
        return firebase_admin.initialize_app(options=options)


def verify_id_token(token: str) -> dict:
    """
    Validate a Firebase ID token and return the decoded claims dict.

    Raises:
        firebase_admin.auth.InvalidIdTokenError — token is malformed or expired
        firebase_admin.auth.RevokedIdTokenError — token has been revoked
        ValueError — token is empty
    """
    _init_firebase()
    return auth.verify_id_token(token)
