"""Integrity tests for the canonical synthetic ESIP fixture graph."""

from __future__ import annotations

from collections import Counter

from app.reference_data import fixtures


def test_store_graph_is_deterministic_and_balanced() -> None:
    assert len(fixtures.BRANDS) == 12
    assert fixtures.REFERENCE_STORE_COUNT == 60
    assert len(fixtures.STORES) == fixtures.REFERENCE_STORE_COUNT
    assert len(fixtures.STORE_BY_ID) == fixtures.REFERENCE_STORE_COUNT

    assert fixtures.STORES[0].store_id == "2001"
    assert fixtures.STORES[-1].store_id == "2060"
    assert len({store.organization_id for store in fixtures.STORES}) == fixtures.REFERENCE_STORE_COUNT
    assert Counter(store.brand for store in fixtures.STORES) == {
        brand: fixtures.STORES_PER_BRAND for brand in fixtures.BRANDS
    }

    for store in fixtures.STORES:
        assert store.name == f"{store.brand} {store.store_id}"
        assert store.brand in fixtures.BRANDS
        assert store.city
        assert store.region
        assert store.pos_type in {"Simphony", "Micros 5.5"}


def test_ticket_graph_references_only_canonical_entities() -> None:
    assert fixtures.REFERENCE_TICKET_COUNT == 240
    assert len(fixtures.SUPPORT_TICKETS) == fixtures.REFERENCE_TICKET_COUNT
    assert len(fixtures.TICKET_BY_ID) == fixtures.REFERENCE_TICKET_COUNT

    assert fixtures.SUPPORT_TICKETS[0].ticket_id == 950001
    assert fixtures.SUPPORT_TICKETS[-1].ticket_id == 950240
    assert Counter(ticket.store_id for ticket in fixtures.SUPPORT_TICKETS) == {
        store.store_id: fixtures.TICKETS_PER_STORE for store in fixtures.STORES
    }

    for ticket in fixtures.SUPPORT_TICKETS:
        assert ticket.store_id in fixtures.STORE_BY_ID
        assert ticket.owner_email in fixtures.USER_BY_EMAIL
        assert ticket.subject
        assert ticket.status in {"open", "pending", "solved", "hold"}


def test_identity_aliases_resolve_to_active_reference_users() -> None:
    assert len(fixtures.USERS) == 12
    assert len(fixtures.USER_BY_EMAIL) == len(fixtures.USERS)
    assert all(user.email.endswith(f"@{fixtures.IDENTITY_DOMAIN}") for user in fixtures.USERS)
    assert all(email in fixtures.USER_BY_EMAIL for email in fixtures.LEGACY_IDENTITY_ALIASES.values())
