"""Focused authorization matrix for ZAF legacy identity canonicalization."""

from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
from app.services import firestore


client = TestClient(app)


def _zaf_payload(email: str) -> dict[str, str]:
    return {"email": email, "name": "Alex Morgan", "role": "agent"}


def test_resolve_active_legacy_alias_to_exact_tenant_identity():
    with mock.patch("app.services.firestore.is_active_user", return_value=True) as active:
        resolved = firestore.resolve_zaf_identity_email("jordan.legacy@support.northstar.example.com")

    assert resolved == "jordan.lee@northstar.example.com"
    active.assert_called_once_with("jordan.lee@northstar.example.com")


def test_resolve_missing_or_inactive_legacy_alias_to_none():
    with mock.patch("app.services.firestore.is_active_user", return_value=False):
        assert firestore.resolve_zaf_identity_email("alex.legacy@support.northstar.example.com") is None


def test_resolver_does_not_alias_other_domains_or_canonical_tenant_users():
    with mock.patch("app.services.firestore.is_active_user") as active:
        assert firestore.resolve_zaf_identity_email("alex.morgan@northstar.example.com") == "alex.morgan@northstar.example.com"
        assert firestore.resolve_zaf_identity_email("casey.nguyen@northstar.example.com") == "casey.nguyen@northstar.example.com"

    active.assert_not_called()


def test_active_legacy_alias_creates_canonical_session_and_identity():
    with (
        mock.patch(
            "app.routers.zaf_auth.firestore.resolve_zaf_identity_email",
            return_value="alex.morgan@northstar.example.com",
        ),
        mock.patch("app.routers.zaf_auth.firestore.is_maintenance_blocked", return_value=False) as maintenance,
        mock.patch("app.routers.zaf_auth.firestore.is_user_allowed", return_value=True) as allowed,
        mock.patch("app.routers.zaf_auth.firestore.sync_user_identity_from_auth") as sync,
        mock.patch("app.routers.zaf_auth.firestore.create_zaf_session", return_value="session-token") as session,
    ):
        response = client.post("/api/zaf/auth", json=_zaf_payload("alex.legacy@support.northstar.example.com"))

    assert response.status_code == 200
    assert response.json()["email"] == "alex.morgan@northstar.example.com"
    maintenance.assert_called_once_with("alex.morgan@northstar.example.com")
    allowed.assert_called_once_with("alex.morgan@northstar.example.com")
    sync.assert_called_once_with(email="alex.morgan@northstar.example.com", name="Alex Morgan", source="zaf")
    session.assert_called_once_with("alex.morgan@northstar.example.com", "Alex Morgan", "agent")


def test_missing_legacy_alias_is_denied_with_safe_reason():
    with (
        mock.patch("app.routers.zaf_auth.firestore.resolve_zaf_identity_email", return_value=None),
        mock.patch("app.routers.zaf_auth.firestore.is_maintenance_blocked") as maintenance,
    ):
        with mock.patch("app.routers.zaf_auth.logger.warning") as warning:
            response = client.post("/api/zaf/auth", json=_zaf_payload("missing.legacy@support.northstar.example.com"))

    assert response.status_code == 403
    maintenance.assert_not_called()
    warning.assert_called_once_with(
        "ZAF auth denied for %s (role=%s, reason=%s)",
        "missing.legacy@support.northstar.example.com",
        "agent",
        "unresolved_legacy_alias",
    )


def test_inactive_legacy_alias_is_denied_with_same_safe_reason():
    with mock.patch("app.routers.zaf_auth.firestore.resolve_zaf_identity_email", return_value=None):
        response = client.post("/api/zaf/auth", json=_zaf_payload("inactive.legacy@support.northstar.example.com"))

    assert response.status_code == 403


def test_unrelated_domain_remains_bad_domain():
    with mock.patch("app.routers.zaf_auth.firestore.resolve_zaf_identity_email", return_value="vendor.user@external.example.org"):
        response = client.post("/api/zaf/auth", json=_zaf_payload("vendor.user@external.example.org"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Access restricted to approved Northstar accounts."


def test_maintenance_uses_canonical_identity_after_alias_resolution():
    with (
        mock.patch(
            "app.routers.zaf_auth.firestore.resolve_zaf_identity_email",
            return_value="alex.morgan@northstar.example.com",
        ),
        mock.patch("app.routers.zaf_auth.firestore.is_maintenance_blocked", return_value=True) as maintenance,
    ):
        response = client.post("/api/zaf/auth", json=_zaf_payload("alex.legacy@support.northstar.example.com"))

    assert response.status_code == 503
    maintenance.assert_called_once_with("alex.morgan@northstar.example.com")
