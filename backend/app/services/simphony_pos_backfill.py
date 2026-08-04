"""Deterministic helpers for the Simphony-only Zendesk POS Type backfill."""

from __future__ import annotations

import csv
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

SIMPHONY_REGISTRY_VALUES = {"simphony", "simphony 2"}
TARGET_ZENDESK_POS_TYPE = "Simphony"

CANDIDATE_EXPORT_FIELDS = [
    "org_id",
    "org_name",
    "org_url",
    "store_id",
    "brand",
    "current_pos_type",
    "registry_pos_system_name",
    "registry_pos_type",
    "proposed_pos_type",
    "reason",
    "ticket_count",
    "open_ticket_count",
]
APPLY_ALLOWED_REASONS = {"missing_pos_type", "pos_type_mismatch"}
ZENDESK_UPDATE_MAX_RETRIES = 3
ZENDESK_UPDATE_BACKOFF_SECONDS = 5


@dataclass(frozen=True)
class RegistryPosRecord:
    source_location_id: str
    pos_system_name: str
    pos_type: str = ""
    source_row_number: int | None = None

    @property
    def store_id(self) -> str:
        return normalize_store_id(self.source_location_id)


@dataclass(frozen=True)
class ZendeskProfileRecord:
    org_id: int
    org_name: str
    store_id: str
    pos_type: str = ""
    brand: str = ""
    ticket_count: int = 0
    open_ticket_count: int = 0
    simphony_store: bool | None = None


@dataclass(frozen=True)
class SimphonyPosCandidate:
    org_id: int
    org_name: str
    org_url: str
    store_id: str
    brand: str
    current_pos_type: str
    registry_pos_system_name: str
    registry_pos_type: str
    proposed_pos_type: str
    reason: str
    ticket_count: int
    open_ticket_count: int

    def as_export_row(self) -> dict[str, object]:
        return {
            "org_id": self.org_id,
            "org_name": self.org_name,
            "org_url": self.org_url,
            "store_id": self.store_id,
            "brand": self.brand,
            "current_pos_type": self.current_pos_type,
            "registry_pos_system_name": self.registry_pos_system_name,
            "registry_pos_type": self.registry_pos_type,
            "proposed_pos_type": self.proposed_pos_type,
            "reason": self.reason,
            "ticket_count": self.ticket_count,
            "open_ticket_count": self.open_ticket_count,
        }


@dataclass(frozen=True)
class CandidateBuildResult:
    candidates: tuple[SimphonyPosCandidate, ...]
    unmatched_registry_records: tuple[RegistryPosRecord, ...]
    excluded_registry_records: tuple[RegistryPosRecord, ...]
    already_canonical_profiles: int

    @property
    def reason_counts(self) -> dict[str, int]:
        return dict(Counter(candidate.reason for candidate in self.candidates))


class ManifestValidationError(ValueError):
    """Raised when a reviewed manifest row is unsafe to apply."""


def clean_string(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_store_id(value: object) -> str:
    text = clean_string(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.0)?", text):
        text = str(int(float(text)))
    match = re.search(r"\d{4}", text)
    return match.group(0) if match else text


def normalize_registry_pos_system(value: object) -> str:
    return clean_string(value).casefold()


def proposed_pos_type_for_registry_value(value: object) -> str | None:
    if normalize_registry_pos_system(value) in SIMPHONY_REGISTRY_VALUES:
        return TARGET_ZENDESK_POS_TYPE
    return None


def organization_agent_url(org_id: int, zendesk_base_url: str) -> str:
    base = zendesk_base_url.rstrip("/")
    if base.endswith("/api/v2"):
        base = base[: -len("/api/v2")]
    return f"{base}/agent/organizations/{int(org_id)}"


def registry_record_from_row(row: dict[str, object], *, row_number: int | None = None) -> RegistryPosRecord:
    return RegistryPosRecord(
        source_location_id=clean_string(row.get("SourceLocationID")),
        pos_system_name=clean_string(row.get("POSSystemName")),
        pos_type=clean_string(row.get("POSType")),
        source_row_number=row_number,
    )


def profile_record_from_row(row: dict[str, object]) -> ZendeskProfileRecord:
    return ZendeskProfileRecord(
        org_id=int(row["org_id"]),
        org_name=clean_string(row.get("org_name")),
        store_id=normalize_store_id(row.get("store_id")),
        brand=clean_string(row.get("brand")),
        pos_type=clean_string(row.get("pos_type")),
        ticket_count=_safe_int(row.get("ticket_count")),
        open_ticket_count=_safe_int(row.get("open_ticket_count")),
        simphony_store=_safe_bool(row.get("simphony_store")),
    )


def build_simphony_pos_candidates(
    *,
    registry_records: Iterable[RegistryPosRecord],
    profile_records: Iterable[ZendeskProfileRecord],
    zendesk_base_url: str,
) -> CandidateBuildResult:
    profiles_by_store: dict[str, list[ZendeskProfileRecord]] = {}
    for profile in profile_records:
        store_id = normalize_store_id(profile.store_id)
        if store_id:
            profiles_by_store.setdefault(store_id, []).append(profile)

    simphony_registry_by_store: dict[str, RegistryPosRecord] = {}
    excluded: list[RegistryPosRecord] = []
    for registry_record in registry_records:
        target = proposed_pos_type_for_registry_value(registry_record.pos_system_name)
        store_id = registry_record.store_id
        if not target or not store_id:
            excluded.append(registry_record)
            continue
        existing = simphony_registry_by_store.get(store_id)
        if existing is None or normalize_registry_pos_system(registry_record.pos_system_name) == "simphony 2":
            simphony_registry_by_store[store_id] = registry_record

    candidates: list[SimphonyPosCandidate] = []
    unmatched: list[RegistryPosRecord] = []
    already_canonical = 0
    for store_id in sorted(simphony_registry_by_store):
        registry_record = simphony_registry_by_store[store_id]
        matching_profiles = sorted(
            profiles_by_store.get(store_id) or [],
            key=lambda profile: (profile.org_name.casefold(), profile.org_id),
        )
        if not matching_profiles:
            unmatched.append(registry_record)
            continue
        for profile in matching_profiles:
            current_pos = clean_string(profile.pos_type)
            if current_pos.casefold() == TARGET_ZENDESK_POS_TYPE.casefold():
                already_canonical += 1
                continue
            reason = "missing_pos_type" if not current_pos else "pos_type_mismatch"
            candidates.append(
                SimphonyPosCandidate(
                    org_id=profile.org_id,
                    org_name=profile.org_name,
                    org_url=organization_agent_url(profile.org_id, zendesk_base_url),
                    store_id=store_id,
                    brand=profile.brand,
                    current_pos_type=current_pos,
                    registry_pos_system_name=registry_record.pos_system_name,
                    registry_pos_type=registry_record.pos_type,
                    proposed_pos_type=TARGET_ZENDESK_POS_TYPE,
                    reason=reason,
                    ticket_count=profile.ticket_count,
                    open_ticket_count=profile.open_ticket_count,
                )
            )

    candidates.sort(key=lambda item: (item.store_id, item.org_name.casefold(), item.org_id))
    return CandidateBuildResult(
        candidates=tuple(candidates),
        unmatched_registry_records=tuple(sorted(unmatched, key=lambda item: item.store_id)),
        excluded_registry_records=tuple(excluded),
        already_canonical_profiles=already_canonical,
    )


def write_candidate_csv(path: str | Path, candidates: Iterable[SimphonyPosCandidate]) -> int:
    rows = [candidate.as_export_row() for candidate in candidates]
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANDIDATE_EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def candidate_from_manifest_row(row: dict[str, object]) -> SimphonyPosCandidate:
    return SimphonyPosCandidate(
        org_id=_safe_int_required(row.get("org_id"), "org_id"),
        org_name=clean_string(row.get("org_name")),
        org_url=clean_string(row.get("org_url")),
        store_id=normalize_store_id(row.get("store_id")),
        brand=clean_string(row.get("brand")),
        current_pos_type=clean_string(row.get("current_pos_type")),
        registry_pos_system_name=clean_string(row.get("registry_pos_system_name")),
        registry_pos_type=clean_string(row.get("registry_pos_type")),
        proposed_pos_type=clean_string(row.get("proposed_pos_type")),
        reason=clean_string(row.get("reason")),
        ticket_count=_safe_int(row.get("ticket_count")),
        open_ticket_count=_safe_int(row.get("open_ticket_count")),
    )


def load_candidate_manifest(path: str | Path) -> tuple[SimphonyPosCandidate, ...]:
    manifest_path = Path(path)
    with manifest_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing_fields = sorted(set(CANDIDATE_EXPORT_FIELDS) - set(reader.fieldnames or []))
        if missing_fields:
            raise ManifestValidationError(
                f"Manifest is missing required column(s): {', '.join(missing_fields)}"
            )
        return tuple(candidate_from_manifest_row(row) for row in reader)


def validate_candidate_for_apply(candidate: SimphonyPosCandidate) -> None:
    if candidate.proposed_pos_type != TARGET_ZENDESK_POS_TYPE:
        raise ManifestValidationError(
            f"Refusing org {candidate.org_id}: proposed_pos_type must be exact "
            f"{TARGET_ZENDESK_POS_TYPE!r}, got {candidate.proposed_pos_type!r}"
        )
    if normalize_registry_pos_system(candidate.registry_pos_system_name) not in SIMPHONY_REGISTRY_VALUES:
        raise ManifestValidationError(
            f"Refusing org {candidate.org_id}: REGISTRY POSSystemName is out of scope: "
            f"{candidate.registry_pos_system_name!r}"
        )
    if candidate.reason not in APPLY_ALLOWED_REASONS:
        raise ManifestValidationError(
            f"Refusing org {candidate.org_id}: reason is out of scope: {candidate.reason!r}"
        )
    if not candidate.org_id:
        raise ManifestValidationError("Refusing manifest row without org_id")
    if not candidate.store_id:
        raise ManifestValidationError(f"Refusing org {candidate.org_id}: missing store_id")
    if candidate.current_pos_type.casefold() == TARGET_ZENDESK_POS_TYPE.casefold():
        raise ManifestValidationError(
            f"Refusing org {candidate.org_id}: current_pos_type is already Simphony"
        )


def validate_manifest_for_apply(candidates: Iterable[SimphonyPosCandidate]) -> None:
    seen_org_ids: set[int] = set()
    for candidate in candidates:
        validate_candidate_for_apply(candidate)
        if candidate.org_id in seen_org_ids:
            raise ManifestValidationError(f"Refusing duplicate org_id {candidate.org_id}")
        seen_org_ids.add(candidate.org_id)


def zendesk_update_payload(candidate: SimphonyPosCandidate) -> dict[str, Any]:
    validate_candidate_for_apply(candidate)
    return {
        "organization": {
            "organization_fields": {
                "pos_type": TARGET_ZENDESK_POS_TYPE,
            }
        }
    }


def update_zendesk_org_pos_type(
    *,
    session: requests.Session,
    zendesk_base_url: str,
    candidate: SimphonyPosCandidate,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    payload = zendesk_update_payload(candidate)
    base = zendesk_base_url.rstrip("/")
    url = f"{base}/organizations/{candidate.org_id}.json"
    if "/tickets" in url or "/comments" in url:
        raise RuntimeError(f"Unsafe Zendesk update endpoint: {url}")

    for attempt in range(1, ZENDESK_UPDATE_MAX_RETRIES + 1):
        response = session.put(url, json=payload, timeout=30)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", ZENDESK_UPDATE_BACKOFF_SECONDS))
            sleep_fn(wait)
            continue
        if response.status_code >= 500 and attempt < ZENDESK_UPDATE_MAX_RETRIES:
            sleep_fn(ZENDESK_UPDATE_BACKOFF_SECONDS * attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Zendesk organization update failed after retries: org_id={candidate.org_id}")


def _safe_int(value: object) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _safe_int_required(value: object, field_name: str) -> int:
    result = _safe_int(value)
    if result <= 0:
        raise ManifestValidationError(f"Manifest field {field_name} must be a positive integer")
    return result


def _safe_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None
