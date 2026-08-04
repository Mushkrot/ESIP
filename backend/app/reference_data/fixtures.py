"""Canonical synthetic fixture graph for the ESIP reference environment.

The values in this module are fictional and deterministic. They provide stable
cross-file identities for local development, tests, screenshots, and seeded
reference environments.
"""

from __future__ import annotations

from dataclasses import dataclass


PRODUCT_NAME = "ESIP"
PRODUCT_FULL_NAME = "Enterprise Support Intelligence Platform"
TENANT = "Northstar Hospitality Group"
SUPPORT_ORGANIZATION = "Northstar Support Operations"
IDENTITY_DOMAIN = "northstar.example.com"
LEGACY_IDENTITY_DOMAIN = "support.northstar.example.com"


@dataclass(frozen=True)
class ReferenceUser:
    key: str
    name: str
    email: str
    role: str
    team: str


USERS = (
    ReferenceUser("platform_admin", "Morgan Chen", "morgan.chen@northstar.example.com", "super_admin", "Platform Engineering"),
    ReferenceUser("support_admin", "Priya Shah", "priya.shah@northstar.example.com", "admin", "Support Operations"),
    ReferenceUser("support_engineer_1", "Alex Morgan", "alex.morgan@northstar.example.com", "user", "Service Desk"),
    ReferenceUser("support_engineer_2", "Casey Nguyen", "casey.nguyen@northstar.example.com", "user", "Service Desk"),
    ReferenceUser("reporting_lead", "Jordan Lee", "jordan.lee@northstar.example.com", "admin", "Service Analytics"),
    ReferenceUser("knowledge_manager", "Sofia Reyes", "sofia.reyes@northstar.example.com", "admin", "Knowledge Management"),
    ReferenceUser("pilot_engineer", "Riley Brooks", "riley.brooks@northstar.example.com", "user", "Service Desk"),
    ReferenceUser("operations_analyst", "Noah Bennett", "noah.bennett@northstar.example.com", "user", "Operations Support"),
    ReferenceUser("delivery_analyst", "Taylor Kim", "taylor.kim@northstar.example.com", "user", "Deployment Services"),
    ReferenceUser("support_engineer_3", "Samir Patel", "samir.patel@northstar.example.com", "user", "Service Desk"),
    ReferenceUser("quality_lead", "Olivia Parker", "olivia.parker@northstar.example.com", "admin", "Quality Engineering"),
    ReferenceUser("vendor_liaison", "Jamie Rivera", "jamie.rivera@northstar.example.com", "user", "Vendor Management"),
)

USER_BY_KEY = {user.key: user for user in USERS}
USER_BY_EMAIL = {user.email: user for user in USERS}

PLATFORM_ADMIN_EMAIL = USER_BY_KEY["platform_admin"].email
SUPPORT_ADMIN_EMAIL = USER_BY_KEY["support_admin"].email

LEGACY_IDENTITY_ALIASES = {
    "alex.legacy@support.northstar.example.com": USER_BY_KEY["support_engineer_1"].email,
    "jordan.legacy@support.northstar.example.com": USER_BY_KEY["reporting_lead"].email,
}


@dataclass(frozen=True)
class ReferenceStore:
    store_id: str
    organization_id: int
    brand: str
    name: str
    city: str
    region: str
    pos_type: str


BRANDS = (
    "Cedar Grill",
    "Metro Burger",
    "Frontier Kitchen",
    "Lakeside Tavern",
    "Harbor Chicken",
    "Summit Burger",
    "Morning Brew",
    "Piazza Kitchen",
    "City Fries",
    "Foundry Burger",
    "Fresh Market",
    "Garden Table",
)

BRAND_TAG_MAP = {
    **{brand: brand for brand in BRANDS},
    "cedar_grill": "Cedar Grill",
    "metro_burger": "Metro Burger",
    "frontier_kitchen": "Frontier Kitchen",
    "lakeside_tavern": "Lakeside Tavern",
    "harbor_chicken": "Harbor Chicken",
    "summit_burger": "Summit Burger",
    "morning_brew": "Morning Brew",
    "piazza_kitchen": "Piazza Kitchen",
    "city_fries": "City Fries",
    "foundry_burger": "Foundry Burger",
    "fresh_market": "Fresh Market",
    "garden_table": "Garden Table",
}

BRAND_MARKERS = {
    brand: tuple({brand.casefold(), brand.casefold().replace(" ", "_")})
    for brand in BRANDS
}

_BRAND_LOOKUP = {key.casefold(): value for key, value in BRAND_TAG_MAP.items()}


def canonical_brand(value: object) -> str | None:
    """Return the canonical fixture brand for a tag or display value."""
    text = str(value or "").strip()
    return _BRAND_LOOKUP.get(text.casefold()) if text else None


def brand_from_organization_name(value: object) -> str | None:
    """Resolve the first canonical brand mentioned in an organization name."""
    text = str(value or "").casefold()
    return next((brand for brand in BRANDS if brand.casefold() in text), None)

_STORE_CITIES = (
    ("Northbridge", "East"),
    ("Lakewood", "East"),
    ("Fairview", "Central"),
    ("Brookfield", "Central"),
    ("Riverton", "West"),
    ("Hillcrest", "West"),
    ("Maple Junction", "North"),
    ("Cedar Falls", "North"),
    ("Stonehaven", "South"),
    ("Harbor Point", "South"),
    ("Pine Ridge", "Central"),
    ("Westhaven", "West"),
)

STORES_PER_BRAND = 5
REFERENCE_STORE_COUNT = len(BRANDS) * STORES_PER_BRAND


def _build_store(index: int) -> ReferenceStore:
    brand = BRANDS[index % len(BRANDS)]
    location_index = (index % len(BRANDS) + (index // len(BRANDS)) * 5) % len(_STORE_CITIES)
    city, region = _STORE_CITIES[location_index]
    return ReferenceStore(
        store_id=str(2001 + index),
        organization_id=99_000_000_001 + index,
        brand=brand,
        name=f"{brand} {2001 + index}",
        city=city,
        region=region,
        pos_type="Simphony" if index % 3 else "Micros 5.5",
    )


STORES = tuple(_build_store(index) for index in range(REFERENCE_STORE_COUNT))

STORE_BY_ID = {store.store_id: store for store in STORES}


@dataclass(frozen=True)
class ReferenceSupportTicket:
    ticket_id: int
    store_id: str
    subject: str
    status: str
    owner_email: str


_TICKET_SUBJECTS = (
    "Payment terminal cannot complete authorization",
    "Kitchen display remains offline after restart",
    "Receipt printer queues duplicate jobs",
    "Store network did not fail over to backup circuit",
    "Menu update is missing from two registers",
    "POS workstation rejects valid operator login",
    "Drive-through timer stopped reporting lane events",
    "Back-office workstation cannot reach the reporting portal",
    "Loyalty lookup returns an intermittent timeout",
    "Kitchen bump bar sends duplicate commands",
    "Inventory counts did not synchronize overnight",
    "Mobile ordering tablet cannot join the secure network",
    "Payment settlement report has a missing batch",
    "Digital menu board shows yesterday's pricing",
    "Store manager cannot open the scheduling dashboard",
    "Primary receipt printer reports a cover-open alert",
    "Online order is not reaching the kitchen queue",
    "Guest Wi-Fi access point is repeatedly restarting",
    "Cash drawer does not open after a completed sale",
    "Order confirmation screen is delayed at peak volume",
    "New employee is missing the correct POS role",
    "Vendor dispatch update is not visible on the ticket",
    "Daily sales export contains duplicate rows",
    "Store controller reports low disk capacity",
)

_TICKET_STATUSES = ("open", "pending", "solved", "hold")
_TICKET_OWNERS = (
    USER_BY_KEY["support_engineer_1"].email,
    USER_BY_KEY["support_engineer_2"].email,
    USER_BY_KEY["support_engineer_3"].email,
    USER_BY_KEY["pilot_engineer"].email,
)

TICKETS_PER_STORE = 4
REFERENCE_TICKET_COUNT = len(STORES) * TICKETS_PER_STORE

SUPPORT_TICKETS = tuple(
    ReferenceSupportTicket(
        ticket_id=950_001 + index,
        store_id=STORES[index % len(STORES)].store_id,
        subject=_TICKET_SUBJECTS[index % len(_TICKET_SUBJECTS)],
        status=_TICKET_STATUSES[index % len(_TICKET_STATUSES)],
        owner_email=_TICKET_OWNERS[index % len(_TICKET_OWNERS)],
    )
    for index in range(REFERENCE_TICKET_COUNT)
)

TICKET_BY_ID = {ticket.ticket_id: ticket for ticket in SUPPORT_TICKETS}
