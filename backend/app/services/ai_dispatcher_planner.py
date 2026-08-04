"""Pure schema and parsing helpers for the Universal Chat AI dispatcher plan.

The dispatcher planner boundary is intentionally narrow: a model may propose a
structured task contract, but deterministic backend code validates it before any
execution path can use it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class DispatcherValidationError(ValueError):
    """Raised when caller code needs fail-closed parsing semantics."""


class DispatcherTaskType(str, Enum):
    TICKET_SET_SEARCH = "ticket_set_search"
    TICKET_SET_COMPARE = "ticket_set_compare"
    BULK_TICKET_LOOKUP = "bulk_ticket_lookup"
    FACT_CHECK_PRIOR_ANSWER = "fact_check_prior_answer"
    PROCEDURE_LOOKUP = "procedure_lookup"
    ANALYTICS = "analytics"
    GENERAL_ANSWER = "general_answer"
    CLARIFY = "clarify"


class DispatcherActionName(str, Enum):
    LOAD_PRIOR_RESULT_SET = "load_prior_result_set"
    BULK_TICKET_ID_LOOKUP = "bulk_ticket_id_lookup"
    COMPARE_TICKET_SETS = "compare_ticket_sets"
    TICKET_SET_SEARCH = "ticket_set_search"
    CRITERION_SCORE_TICKET_SET = "criterion_score_ticket_set"
    SAME_STORE_SNAPSHOT = "same_store_snapshot"
    SEARCH_SIMILAR_TICKETS = "search_similar_tickets"
    SEARCH_TICKETS_KEYWORD = "search_tickets_keyword"
    RUN_BQ_QUERY = "run_bq_query"
    SEARCH_DRIVE_EXACT = "search_drive_exact"
    SEARCH_KNOWLEDGE_BASE = "search_knowledge_base"
    ASK_CLARIFICATION = "ask_clarification"


class DispatcherSourceFamily(str, Enum):
    TICKETS = "tickets"
    KB = "kb"
    DRIVE = "drive"
    ANALYTICS = "analytics"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class DispatcherMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ASSISTIVE = "assistive"
    PRIMARY_FOR_ELIGIBLE = "primary_for_eligible"


class DispatcherFallback(str, Enum):
    LEGACY_UNIVERSAL_CHAT = "legacy_universal_chat"
    ASK_CLARIFICATION = "ask_clarification"
    SAFE_LIMITATION = "safe_limitation"


_STRING_ARRAY_SCHEMA: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
_INTEGER_ARRAY_SCHEMA: dict[str, Any] = {"type": "array", "items": {"type": "integer"}}

DISPATCHER_PLAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": [item.value for item in DispatcherTaskType]},
        "answer_language": {"type": "string"},
        "confidence": {"type": "number"},
        "requires_dispatcher": {"type": "boolean"},
        "scope": {
            "type": "object",
            "properties": {
                "store_ids": _STRING_ARRAY_SCHEMA,
                "ticket_ids": _INTEGER_ARRAY_SCHEMA,
                "period": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                },
                "source_family": {"type": "string", "enum": [item.value for item in DispatcherSourceFamily]},
            },
            "required": ["store_ids", "ticket_ids", "source_family"],
        },
        "prior_result_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn_id": {"type": "string"},
                    "use_full_result_set": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["turn_id", "use_full_result_set", "reason"],
            },
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [item.value for item in DispatcherActionName]},
                    "inputs": {
                        "type": "object",
                        "properties": {
                            "ticket_ids": _INTEGER_ARRAY_SCHEMA,
                            "store_ids": _STRING_ARRAY_SCHEMA,
                            "turn_id": {"type": "string"},
                            "criteria": _STRING_ARRAY_SCHEMA,
                            "query": {"type": "string"},
                        },
                    },
                    "max_rows": {"type": "integer"},
                    "required": {"type": "boolean"},
                },
                "required": ["name", "inputs", "max_rows", "required"],
            },
        },
        "comparison_contract": {
            "type": "object",
            "properties": {
                "left_label": {"type": "string"},
                "right_label": {"type": "string"},
                "criteria": _STRING_ARRAY_SCHEMA,
                "must_report": _STRING_ARRAY_SCHEMA,
            },
            "required": ["left_label", "right_label", "criteria", "must_report"],
        },
        "answer_contract": {
            "type": "object",
            "properties": {
                "must_include": _STRING_ARRAY_SCHEMA,
                "must_not_claim": _STRING_ARRAY_SCHEMA,
            },
            "required": ["must_include", "must_not_claim"],
        },
        "risk_flags": _STRING_ARRAY_SCHEMA,
        "cost_plan": {
            "type": "object",
            "properties": {
                "max_planner_calls": {"type": "integer"},
                "max_tool_actions": {"type": "integer"},
                "max_ticket_ids": {"type": "integer"},
                "max_rows": {"type": "integer"},
            },
            "required": ["max_planner_calls", "max_tool_actions", "max_ticket_ids", "max_rows"],
        },
        "fallback": {"type": "string", "enum": [item.value for item in DispatcherFallback]},
    },
    "required": [
        "task_type",
        "answer_language",
        "confidence",
        "requires_dispatcher",
        "scope",
        "prior_result_refs",
        "actions",
        "comparison_contract",
        "answer_contract",
        "risk_flags",
        "cost_plan",
        "fallback",
    ],
}

_TASK_TYPE_ALIASES = {
    "bulk_ticket_id_lookup": DispatcherTaskType.BULK_TICKET_LOOKUP.value,
    "bulk_ticket_ids": DispatcherTaskType.BULK_TICKET_LOOKUP.value,
    "ticket_id_lookup": DispatcherTaskType.BULK_TICKET_LOOKUP.value,
    "ticket_lookup": DispatcherTaskType.BULK_TICKET_LOOKUP.value,
    "compare_ticket_sets": DispatcherTaskType.TICKET_SET_COMPARE.value,
    "compare_tickets": DispatcherTaskType.TICKET_SET_COMPARE.value,
    "ticket_compare": DispatcherTaskType.TICKET_SET_COMPARE.value,
    "ticket_set_comparison": DispatcherTaskType.TICKET_SET_COMPARE.value,
    "prior_result_compare": DispatcherTaskType.TICKET_SET_COMPARE.value,
    "verify_prior_answer": DispatcherTaskType.FACT_CHECK_PRIOR_ANSWER.value,
    "challenge_fact_check": DispatcherTaskType.FACT_CHECK_PRIOR_ANSWER.value,
    "fact_check": DispatcherTaskType.FACT_CHECK_PRIOR_ANSWER.value,
    "multi_store_ticket_search": DispatcherTaskType.TICKET_SET_SEARCH.value,
    "ticket_search": DispatcherTaskType.TICKET_SET_SEARCH.value,
    "answer": DispatcherTaskType.GENERAL_ANSWER.value,
}

_ACTION_NAME_ALIASES = {
    "bulk_ticket_ids": DispatcherActionName.BULK_TICKET_ID_LOOKUP.value,
    "bulk_ticket_lookup": DispatcherActionName.BULK_TICKET_ID_LOOKUP.value,
    "lookup_ticket_ids": DispatcherActionName.BULK_TICKET_ID_LOOKUP.value,
    "ticket_id_lookup": DispatcherActionName.BULK_TICKET_ID_LOOKUP.value,
    "get_ticket_details": DispatcherActionName.BULK_TICKET_ID_LOOKUP.value,
    "compare_tickets": DispatcherActionName.COMPARE_TICKET_SETS.value,
    "compare_ticket_ids": DispatcherActionName.COMPARE_TICKET_SETS.value,
    "load_previous_result_set": DispatcherActionName.LOAD_PRIOR_RESULT_SET.value,
    "load_previous_results": DispatcherActionName.LOAD_PRIOR_RESULT_SET.value,
    "prior_result_set": DispatcherActionName.LOAD_PRIOR_RESULT_SET.value,
    "ticket_search": DispatcherActionName.TICKET_SET_SEARCH.value,
    "multi_store_ticket_search": DispatcherActionName.TICKET_SET_SEARCH.value,
    "same_store_history": DispatcherActionName.SAME_STORE_SNAPSHOT.value,
    "similar_tickets": DispatcherActionName.SEARCH_SIMILAR_TICKETS.value,
    "keyword_ticket_search": DispatcherActionName.SEARCH_TICKETS_KEYWORD.value,
    "bq_query": DispatcherActionName.RUN_BQ_QUERY.value,
    "drive_search": DispatcherActionName.SEARCH_DRIVE_EXACT.value,
    "kb_search": DispatcherActionName.SEARCH_KNOWLEDGE_BASE.value,
    "clarify": DispatcherActionName.ASK_CLARIFICATION.value,
}

_SOURCE_FAMILY_ALIASES = {
    "ticket": DispatcherSourceFamily.TICKETS.value,
    "zendesk": DispatcherSourceFamily.TICKETS.value,
    "zendesk_tickets": DispatcherSourceFamily.TICKETS.value,
    "knowledge_base": DispatcherSourceFamily.KB.value,
    "kb_articles": DispatcherSourceFamily.KB.value,
    "google_drive": DispatcherSourceFamily.DRIVE.value,
    "bq": DispatcherSourceFamily.ANALYTICS.value,
    "bigquery": DispatcherSourceFamily.ANALYTICS.value,
    "global": DispatcherSourceFamily.UNKNOWN.value,
    "none": DispatcherSourceFamily.UNKNOWN.value,
    "not_applicable": DispatcherSourceFamily.UNKNOWN.value,
}

_FALLBACK_ALIASES = {
    "legacy": DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value,
    "legacy_chat": DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value,
    "legacy_universal": DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value,
    "current_universal_chat": DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value,
    "universal_chat": DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value,
    "fallback": DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value,
    "clarify": DispatcherFallback.ASK_CLARIFICATION.value,
    "limitation": DispatcherFallback.SAFE_LIMITATION.value,
}

_CONFIDENCE_LABELS = {
    "very_high": 0.9,
    "high": 0.8,
    "medium": 0.6,
    "moderate": 0.6,
    "low": 0.4,
    "very_low": 0.2,
}

@dataclass(frozen=True)
class DispatcherValidationIssue:
    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class DispatcherPeriod:
    start: str | None = None
    end: str | None = None
    timezone: str = "America/New_York"

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "timezone": self.timezone,
        }


@dataclass(frozen=True)
class DispatcherScope:
    store_ids: tuple[str, ...] = ()
    ticket_ids: tuple[int, ...] = ()
    period: DispatcherPeriod | None = None
    source_family: DispatcherSourceFamily = DispatcherSourceFamily.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_ids": list(self.store_ids),
            "ticket_ids": list(self.ticket_ids),
            "period": self.period.to_dict() if self.period else None,
            "source_family": self.source_family.value,
        }


@dataclass(frozen=True)
class DispatcherPriorResultRef:
    turn_id: str | None = None
    use_full_result_set: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "use_full_result_set": self.use_full_result_set,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DispatcherAction:
    name: DispatcherActionName
    inputs: Mapping[str, Any] = field(default_factory=dict)
    max_rows: int | None = None
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "inputs": dict(self.inputs),
            "max_rows": self.max_rows,
            "required": self.required,
        }


@dataclass(frozen=True)
class DispatcherComparisonContract:
    left_label: str = ""
    right_label: str = ""
    criteria: tuple[str, ...] = ()
    must_report: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_label": self.left_label,
            "right_label": self.right_label,
            "criteria": list(self.criteria),
            "must_report": list(self.must_report),
        }


@dataclass(frozen=True)
class DispatcherAnswerContract:
    must_include: tuple[str, ...] = ()
    must_not_claim: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "must_include": list(self.must_include),
            "must_not_claim": list(self.must_not_claim),
        }


@dataclass(frozen=True)
class DispatcherCostPlan:
    max_planner_calls: int = 1
    max_tool_actions: int = 4
    max_ticket_ids: int = 75
    max_rows: int = 100

    def to_dict(self) -> dict[str, int]:
        return {
            "max_planner_calls": self.max_planner_calls,
            "max_tool_actions": self.max_tool_actions,
            "max_ticket_ids": self.max_ticket_ids,
            "max_rows": self.max_rows,
        }


@dataclass(frozen=True)
class DispatcherPlan:
    task_type: DispatcherTaskType
    answer_language: str = "user"
    confidence: float = 0.0
    requires_dispatcher: bool = True
    scope: DispatcherScope = field(default_factory=DispatcherScope)
    prior_result_refs: tuple[DispatcherPriorResultRef, ...] = ()
    actions: tuple[DispatcherAction, ...] = ()
    comparison_contract: DispatcherComparisonContract = field(default_factory=DispatcherComparisonContract)
    answer_contract: DispatcherAnswerContract = field(default_factory=DispatcherAnswerContract)
    risk_flags: tuple[str, ...] = ()
    cost_plan: DispatcherCostPlan = field(default_factory=DispatcherCostPlan)
    fallback: DispatcherFallback = DispatcherFallback.LEGACY_UNIVERSAL_CHAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type.value,
            "answer_language": self.answer_language,
            "confidence": self.confidence,
            "requires_dispatcher": self.requires_dispatcher,
            "scope": self.scope.to_dict(),
            "prior_result_refs": [ref.to_dict() for ref in self.prior_result_refs],
            "actions": [action.to_dict() for action in self.actions],
            "comparison_contract": self.comparison_contract.to_dict(),
            "answer_contract": self.answer_contract.to_dict(),
            "risk_flags": list(self.risk_flags),
            "cost_plan": self.cost_plan.to_dict(),
            "fallback": self.fallback.value,
        }


@dataclass(frozen=True)
class DispatcherParseResult:
    plan: DispatcherPlan | None = None
    issues: tuple[DispatcherValidationIssue, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.plan is not None and not self.issues

    @property
    def is_valid(self) -> bool:
        return self.accepted

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "plan": self.plan.to_dict() if self.plan else None,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class DispatcherPolicyLimits:
    max_planner_calls: int = 1
    max_tool_actions: int = 4
    max_explicit_ticket_ids: int = 75
    max_action_rows: int = 100

    def to_dict(self) -> dict[str, int]:
        return {
            "max_planner_calls": self.max_planner_calls,
            "max_tool_actions": self.max_tool_actions,
            "max_explicit_ticket_ids": self.max_explicit_ticket_ids,
            "max_action_rows": self.max_action_rows,
        }


@dataclass(frozen=True)
class DispatcherDeterministicEntities:
    store_ids: tuple[str, ...] = ()
    ticket_ids: tuple[int, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_ids": list(self.store_ids),
            "ticket_ids": list(self.ticket_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DispatcherCapDetail:
    field: str
    code: str
    configured_limit: int
    requested: int
    applied: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "code": self.code,
            "configured_limit": self.configured_limit,
            "requested": self.requested,
            "applied": self.applied,
        }


@dataclass(frozen=True)
class DispatcherCompiledAction:
    name: DispatcherActionName
    inputs: Mapping[str, Any] = field(default_factory=dict)
    max_rows: int = 100
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "inputs": dict(self.inputs),
            "max_rows": self.max_rows,
            "required": self.required,
        }


@dataclass(frozen=True)
class DispatcherCompileDecision:
    accepted: bool
    mode: DispatcherMode
    fallback: DispatcherFallback
    task_type: DispatcherTaskType | None = None
    execute_actions: bool = False
    effective_scope: DispatcherScope = field(default_factory=DispatcherScope)
    actions: tuple[DispatcherCompiledAction, ...] = ()
    reason_codes: tuple[str, ...] = ()
    issues: tuple[DispatcherValidationIssue, ...] = ()
    cap_details: tuple[DispatcherCapDetail, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "mode": self.mode.value,
            "fallback": self.fallback.value,
            "task_type": self.task_type.value if self.task_type else "",
            "execute_actions": self.execute_actions,
            "effective_scope": self.effective_scope.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "reason_codes": list(self.reason_codes),
            "issues": [issue.to_dict() for issue in self.issues],
            "cap_details": [detail.to_dict() for detail in self.cap_details],
        }


def parse_dispatcher_plan(payload: Mapping[str, Any] | str) -> DispatcherParseResult:
    """Parse a planner proposal into a typed contract or redacted issues."""
    issues: list[DispatcherValidationIssue] = []
    data = _coerce_mapping(payload, "payload", issues)
    if issues:
        return DispatcherParseResult(issues=tuple(issues))
    data = _normalize_payload_shape(data)

    task_type = _enum_value(DispatcherTaskType, data.get("task_type"), "task_type", issues)
    fallback = _enum_value(
        DispatcherFallback,
        data.get("fallback", DispatcherFallback.LEGACY_UNIVERSAL_CHAT.value),
        "fallback",
        issues,
    )
    confidence = _confidence(data.get("confidence", 0.0), "confidence", issues)
    scope = _parse_scope(data.get("scope", {}), issues)
    prior_result_refs = _parse_prior_result_refs(data.get("prior_result_refs", []), issues)
    actions = _parse_actions(data.get("actions", []), issues)
    comparison_contract = _parse_comparison_contract(data.get("comparison_contract", {}), issues)
    answer_contract = _parse_answer_contract(data.get("answer_contract", {}), issues)
    cost_plan = _parse_cost_plan(data.get("cost_plan", {}), issues)

    if issues or task_type is None or fallback is None:
        return DispatcherParseResult(issues=tuple(issues))

    plan = DispatcherPlan(
        task_type=task_type,
        answer_language=_clean_str(data.get("answer_language")) or "user",
        confidence=confidence,
        requires_dispatcher=bool(data.get("requires_dispatcher", True)),
        scope=scope,
        prior_result_refs=prior_result_refs,
        actions=actions,
        comparison_contract=comparison_contract,
        answer_contract=answer_contract,
        risk_flags=_clean_str_list(data.get("risk_flags", []), "risk_flags", issues),
        cost_plan=cost_plan,
        fallback=fallback,
    )
    if issues:
        return DispatcherParseResult(issues=tuple(issues))
    return DispatcherParseResult(plan=plan)


def parse_dispatcher_plan_or_raise(payload: Mapping[str, Any] | str) -> DispatcherPlan:
    result = parse_dispatcher_plan(payload)
    if not result.accepted or result.plan is None:
        raise DispatcherValidationError("dispatcher plan validation failed")
    return result.plan


def compile_dispatcher_payload(
    payload: Mapping[str, Any] | str,
    *,
    mode: DispatcherMode | str = DispatcherMode.SHADOW,
    limits: DispatcherPolicyLimits | None = None,
    deterministic_entities: DispatcherDeterministicEntities | None = None,
) -> DispatcherCompileDecision:
    parsed = parse_dispatcher_plan(payload)
    resolved_mode, mode_issue = _resolve_mode(mode)
    if not parsed.accepted or parsed.plan is None:
        return DispatcherCompileDecision(
            accepted=False,
            mode=resolved_mode,
            fallback=DispatcherFallback.LEGACY_UNIVERSAL_CHAT,
            reason_codes=("planner_validation_failed",),
            issues=tuple(list(parsed.issues) + ([mode_issue] if mode_issue else [])),
        )
    return compile_dispatcher_plan(
        parsed.plan,
        mode=resolved_mode,
        limits=limits,
        deterministic_entities=deterministic_entities,
        extra_issues=([mode_issue] if mode_issue else None),
    )


def compile_dispatcher_plan(
    plan: DispatcherPlan,
    *,
    mode: DispatcherMode | str = DispatcherMode.SHADOW,
    limits: DispatcherPolicyLimits | None = None,
    deterministic_entities: DispatcherDeterministicEntities | None = None,
    extra_issues: list[DispatcherValidationIssue] | None = None,
) -> DispatcherCompileDecision:
    resolved_mode, mode_issue = _resolve_mode(mode)
    policy_limits = limits or DispatcherPolicyLimits()
    issues: list[DispatcherValidationIssue] = list(extra_issues or [])
    if mode_issue:
        issues.append(mode_issue)
    cap_details: list[DispatcherCapDetail] = []
    reason_codes: list[str] = []

    if resolved_mode is DispatcherMode.DISABLED:
        return DispatcherCompileDecision(
            accepted=False,
            mode=resolved_mode,
            fallback=DispatcherFallback.LEGACY_UNIVERSAL_CHAT,
            reason_codes=("dispatcher_disabled",),
        )

    effective_scope = _effective_scope(plan, policy_limits, deterministic_entities, issues, cap_details, reason_codes)
    _validate_plan_cost(plan, policy_limits, issues, cap_details, reason_codes)
    if len(plan.actions) > policy_limits.max_tool_actions:
        _add_issue(issues, "actions", "over_limit", "Too many planner actions.")
        reason_codes.append("too_many_actions")
    if not _has_required_scope(plan, effective_scope):
        _add_issue(issues, "scope", "missing_required_scope", "Required ticket or store scope is missing.")
        reason_codes.append("missing_required_scope")

    compiled_actions = _compile_actions(plan.actions, effective_scope, policy_limits, issues, cap_details, reason_codes)
    if issues:
        return DispatcherCompileDecision(
            accepted=False,
            mode=resolved_mode,
            fallback=plan.fallback,
            task_type=plan.task_type,
            effective_scope=effective_scope,
            reason_codes=tuple(dict.fromkeys(reason_codes or ["policy_validation_failed"])),
            issues=tuple(issues),
            cap_details=tuple(cap_details),
        )

    return DispatcherCompileDecision(
        accepted=True,
        mode=resolved_mode,
        fallback=plan.fallback,
        task_type=plan.task_type,
        execute_actions=resolved_mode in {DispatcherMode.ASSISTIVE, DispatcherMode.PRIMARY_FOR_ELIGIBLE},
        effective_scope=effective_scope,
        actions=compiled_actions,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        cap_details=tuple(cap_details),
    )


def _parse_scope(raw: Any, issues: list[DispatcherValidationIssue]) -> DispatcherScope:
    data = _mapping_or_issue(raw, "scope", issues)
    source_family = _enum_value(
        DispatcherSourceFamily,
        data.get("source_family", DispatcherSourceFamily.UNKNOWN.value),
        "scope.source_family",
        issues,
    )
    return DispatcherScope(
        store_ids=_clean_store_ids(data.get("store_ids", []), "scope.store_ids", issues),
        ticket_ids=_clean_ticket_ids(data.get("ticket_ids", []), "scope.ticket_ids", issues),
        period=_parse_period(data.get("period"), issues),
        source_family=source_family or DispatcherSourceFamily.UNKNOWN,
    )


def _normalize_payload_shape(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Clean up common model-output shape drift without accepting unsafe values."""
    normalized = dict(data)

    if "task_type" in normalized:
        normalized["task_type"] = _alias_string(normalized.get("task_type"), _TASK_TYPE_ALIASES)
    if "fallback" in normalized:
        normalized["fallback"] = _alias_string(normalized.get("fallback"), _FALLBACK_ALIASES)
    if "confidence" in normalized:
        normalized["confidence"] = _normalize_confidence_value(normalized.get("confidence"))
    normalized["scope"] = _normalize_scope_shape(normalized.get("scope", {}))
    normalized["actions"] = _normalize_action_shapes(normalized.get("actions", []))
    normalized["comparison_contract"] = _normalize_optional_contract_object(
        normalized.get("comparison_contract", {})
    )
    normalized["answer_contract"] = _normalize_optional_contract_object(normalized.get("answer_contract", {}))
    normalized["cost_plan"] = _normalize_optional_contract_object(normalized.get("cost_plan", {}))
    return normalized


def _normalize_scope_shape(raw: Any) -> Any:
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        return {"source_family": _alias_string(raw, _SOURCE_FAMILY_ALIASES)}
    if not isinstance(raw, Mapping):
        return raw

    normalized = dict(raw)
    source = normalized.get("source_family")
    if source in (None, ""):
        source = normalized.get("source") or normalized.get("source_type") or normalized.get("source_class")
    if source not in (None, ""):
        normalized["source_family"] = _alias_string(source, _SOURCE_FAMILY_ALIASES)
    return normalized


def _normalize_action_shapes(raw: Any) -> Any:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        return raw

    normalized: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            normalized.append(
                {
                    "name": _alias_string(item, _ACTION_NAME_ALIASES),
                    "inputs": {},
                    "max_rows": 100,
                    "required": True,
                }
            )
            continue
        if not isinstance(item, Mapping):
            normalized.append(item)
            continue
        action = dict(item)
        if action.get("name") in (None, ""):
            for alias_key in ("action", "action_name", "tool", "tool_name"):
                if action.get(alias_key) not in (None, ""):
                    action["name"] = action.get(alias_key)
                    break
        if action.get("name") not in (None, ""):
            action["name"] = _alias_string(action.get("name"), _ACTION_NAME_ALIASES)
        if action.get("inputs") in (None, ""):
            action["inputs"] = {}
        normalized.append(action)
    return normalized


def _normalize_optional_contract_object(raw: Any) -> Any:
    if raw in (None, ""):
        return {}
    if isinstance(raw, str) and _alias_key(raw) in {"none", "n_a", "na", "not_applicable"}:
        return {}
    return raw


def _normalize_confidence_value(raw: Any) -> Any:
    if isinstance(raw, str):
        key = _alias_key(raw)
        if key in _CONFIDENCE_LABELS:
            return _CONFIDENCE_LABELS[key]
    return raw


def _alias_string(raw: Any, aliases: Mapping[str, str]) -> Any:
    key = _alias_key(raw)
    if key in aliases:
        return aliases[key]
    return raw


def _alias_key(raw: Any) -> str:
    return _clean_str(raw).casefold().replace("-", "_").replace(" ", "_").replace("/", "_")


def _resolve_mode(mode: DispatcherMode | str) -> tuple[DispatcherMode, DispatcherValidationIssue | None]:
    if isinstance(mode, DispatcherMode):
        return mode, None
    try:
        return DispatcherMode(str(mode).strip()), None
    except ValueError:
        return (
            DispatcherMode.DISABLED,
            DispatcherValidationIssue(field="mode", code="unsupported_value", message="Unsupported value."),
        )


def _effective_scope(
    plan: DispatcherPlan,
    limits: DispatcherPolicyLimits,
    deterministic_entities: DispatcherDeterministicEntities | None,
    issues: list[DispatcherValidationIssue],
    cap_details: list[DispatcherCapDetail],
    reason_codes: list[str],
) -> DispatcherScope:
    action_store_ids = _store_ids_from_actions(plan.actions, issues)
    action_ticket_ids = _ticket_ids_from_actions(plan.actions, issues)
    deterministic_store_ids = deterministic_entities.store_ids if deterministic_entities else ()
    deterministic_ticket_ids = deterministic_entities.ticket_ids if deterministic_entities else ()

    store_ids = _dedupe_strings(
        deterministic_store_ids,
        plan.scope.store_ids,
        action_store_ids,
    )
    ticket_ids = _dedupe_ints(
        deterministic_ticket_ids,
        plan.scope.ticket_ids,
        action_ticket_ids,
    )
    if len(ticket_ids) > limits.max_explicit_ticket_ids:
        cap_details.append(
            DispatcherCapDetail(
                field="scope.ticket_ids",
                code="ticket_ids_trimmed",
                configured_limit=limits.max_explicit_ticket_ids,
                requested=len(ticket_ids),
                applied=limits.max_explicit_ticket_ids,
            )
        )
        reason_codes.append("ticket_ids_trimmed")
        ticket_ids = ticket_ids[: limits.max_explicit_ticket_ids]

    return DispatcherScope(
        store_ids=store_ids,
        ticket_ids=ticket_ids,
        period=plan.scope.period,
        source_family=plan.scope.source_family,
    )


def _validate_plan_cost(
    plan: DispatcherPlan,
    limits: DispatcherPolicyLimits,
    issues: list[DispatcherValidationIssue],
    cap_details: list[DispatcherCapDetail],
    reason_codes: list[str],
) -> None:
    if plan.cost_plan.max_planner_calls > limits.max_planner_calls:
        _add_issue(issues, "cost_plan.max_planner_calls", "over_limit", "Too many planner calls.")
        reason_codes.append("too_many_planner_calls")
    if plan.cost_plan.max_tool_actions > limits.max_tool_actions:
        cap_details.append(
            DispatcherCapDetail(
                field="cost_plan.max_tool_actions",
                code="cost_tool_actions_cap_clamped",
                configured_limit=limits.max_tool_actions,
                requested=plan.cost_plan.max_tool_actions,
                applied=limits.max_tool_actions,
            )
        )
        reason_codes.append("cost_tool_actions_cap_clamped")
    if plan.cost_plan.max_ticket_ids > limits.max_explicit_ticket_ids:
        cap_details.append(
            DispatcherCapDetail(
                field="cost_plan.max_ticket_ids",
                code="cost_ticket_cap_clamped",
                configured_limit=limits.max_explicit_ticket_ids,
                requested=plan.cost_plan.max_ticket_ids,
                applied=limits.max_explicit_ticket_ids,
            )
        )
        reason_codes.append("cost_ticket_cap_clamped")
    if plan.cost_plan.max_rows > limits.max_action_rows:
        cap_details.append(
            DispatcherCapDetail(
                field="cost_plan.max_rows",
                code="cost_rows_cap_clamped",
                configured_limit=limits.max_action_rows,
                requested=plan.cost_plan.max_rows,
                applied=limits.max_action_rows,
            )
        )
        reason_codes.append("cost_rows_cap_clamped")


def _compile_actions(
    actions: tuple[DispatcherAction, ...],
    effective_scope: DispatcherScope,
    limits: DispatcherPolicyLimits,
    issues: list[DispatcherValidationIssue],
    cap_details: list[DispatcherCapDetail],
    reason_codes: list[str],
) -> tuple[DispatcherCompiledAction, ...]:
    compiled: list[DispatcherCompiledAction] = []
    for index, action in enumerate(actions):
        max_rows = action.max_rows if action.max_rows is not None else limits.max_action_rows
        if max_rows > limits.max_action_rows:
            cap_details.append(
                DispatcherCapDetail(
                    field=f"actions[{index}].max_rows",
                    code="action_rows_trimmed",
                    configured_limit=limits.max_action_rows,
                    requested=max_rows,
                    applied=limits.max_action_rows,
                )
            )
            reason_codes.append("action_rows_trimmed")
            max_rows = limits.max_action_rows
        if max_rows <= 0:
            _add_issue(issues, f"actions[{index}].max_rows", "out_of_range", "Expected a positive row cap.")
            reason_codes.append("invalid_action_rows")
            continue

        inputs = dict(action.inputs)
        if action.name in {
            DispatcherActionName.BULK_TICKET_ID_LOOKUP,
            DispatcherActionName.COMPARE_TICKET_SETS,
            DispatcherActionName.CRITERION_SCORE_TICKET_SET,
        }:
            inputs["ticket_ids"] = list(effective_scope.ticket_ids)
        if action.name in {
            DispatcherActionName.TICKET_SET_SEARCH,
            DispatcherActionName.SAME_STORE_SNAPSHOT,
            DispatcherActionName.SEARCH_SIMILAR_TICKETS,
        }:
            inputs["store_ids"] = list(effective_scope.store_ids)

        compiled.append(
            DispatcherCompiledAction(
                name=action.name,
                inputs=inputs,
                max_rows=max_rows,
                required=action.required,
            )
        )
    return tuple(compiled)


def _has_required_scope(plan: DispatcherPlan, effective_scope: DispatcherScope) -> bool:
    has_store_ids = bool(effective_scope.store_ids)
    has_ticket_ids = bool(effective_scope.ticket_ids)
    has_prior_refs = bool(plan.prior_result_refs)
    if plan.task_type is DispatcherTaskType.BULK_TICKET_LOOKUP:
        return has_ticket_ids
    if plan.task_type is DispatcherTaskType.TICKET_SET_SEARCH:
        return has_store_ids or has_ticket_ids
    if plan.task_type in {DispatcherTaskType.TICKET_SET_COMPARE, DispatcherTaskType.FACT_CHECK_PRIOR_ANSWER}:
        return has_prior_refs or has_ticket_ids
    return True


def _store_ids_from_actions(
    actions: tuple[DispatcherAction, ...],
    issues: list[DispatcherValidationIssue],
) -> tuple[str, ...]:
    values: list[str] = []
    for index, action in enumerate(actions):
        raw = action.inputs.get("store_ids")
        if raw in (None, ""):
            continue
        values.extend(_clean_store_ids(raw, f"actions[{index}].inputs.store_ids", issues))
    return tuple(dict.fromkeys(values))


def _ticket_ids_from_actions(
    actions: tuple[DispatcherAction, ...],
    issues: list[DispatcherValidationIssue],
) -> tuple[int, ...]:
    values: list[int] = []
    for index, action in enumerate(actions):
        raw = action.inputs.get("ticket_ids")
        if raw in (None, ""):
            continue
        values.extend(_clean_ticket_ids(raw, f"actions[{index}].inputs.ticket_ids", issues))
    return tuple(dict.fromkeys(values))


def _dedupe_strings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        values.extend(_clean_str(item) for item in group if _clean_str(item))
    return tuple(dict.fromkeys(values))


def _dedupe_ints(*groups: tuple[int, ...]) -> tuple[int, ...]:
    values: list[int] = []
    for group in groups:
        for item in group:
            if isinstance(item, bool):
                continue
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                values.append(value)
    return tuple(dict.fromkeys(values))


def _parse_period(raw: Any, issues: list[DispatcherValidationIssue]) -> DispatcherPeriod | None:
    if raw in (None, ""):
        return None
    data = _mapping_or_issue(raw, "scope.period", issues)
    if not data:
        return None
    return DispatcherPeriod(
        start=_clean_str(data.get("start")) or None,
        end=_clean_str(data.get("end")) or None,
        timezone=_clean_str(data.get("timezone")) or "America/New_York",
    )


def _parse_prior_result_refs(
    raw: Any,
    issues: list[DispatcherValidationIssue],
) -> tuple[DispatcherPriorResultRef, ...]:
    refs = _list_of_mappings(raw, "prior_result_refs", issues)
    parsed: list[DispatcherPriorResultRef] = []
    for index, item in enumerate(refs):
        parsed.append(
            DispatcherPriorResultRef(
                turn_id=_clean_str(item.get("turn_id")) or None,
                use_full_result_set=bool(item.get("use_full_result_set", False)),
                reason=_clean_str(item.get("reason")),
            )
        )
        if not isinstance(item.get("use_full_result_set", False), bool):
            _add_issue(issues, f"prior_result_refs[{index}].use_full_result_set", "invalid_type", "Expected a boolean.")
    return tuple(parsed)


def _parse_actions(raw: Any, issues: list[DispatcherValidationIssue]) -> tuple[DispatcherAction, ...]:
    action_items = _list_of_mappings(raw, "actions", issues)
    parsed: list[DispatcherAction] = []
    for index, item in enumerate(action_items):
        field = f"actions[{index}]"
        name = _enum_value(DispatcherActionName, item.get("name"), f"{field}.name", issues)
        inputs = _mapping_or_issue(item.get("inputs", {}), f"{field}.inputs", issues)
        max_rows = _optional_positive_int(item.get("max_rows"), f"{field}.max_rows", issues)
        required = item.get("required", False)
        if not isinstance(required, bool):
            _add_issue(issues, f"{field}.required", "invalid_type", "Expected a boolean.")
        if name is None:
            continue
        parsed.append(
            DispatcherAction(
                name=name,
                inputs=dict(inputs),
                max_rows=max_rows,
                required=bool(required),
            )
        )
    return tuple(parsed)


def _parse_comparison_contract(
    raw: Any,
    issues: list[DispatcherValidationIssue],
) -> DispatcherComparisonContract:
    data = _mapping_or_issue(raw, "comparison_contract", issues)
    return DispatcherComparisonContract(
        left_label=_clean_str(data.get("left_label")),
        right_label=_clean_str(data.get("right_label")),
        criteria=_clean_str_list(data.get("criteria", []), "comparison_contract.criteria", issues),
        must_report=_clean_str_list(data.get("must_report", []), "comparison_contract.must_report", issues),
    )


def _parse_answer_contract(raw: Any, issues: list[DispatcherValidationIssue]) -> DispatcherAnswerContract:
    data = _mapping_or_issue(raw, "answer_contract", issues)
    return DispatcherAnswerContract(
        must_include=_clean_str_list(data.get("must_include", []), "answer_contract.must_include", issues),
        must_not_claim=_clean_str_list(data.get("must_not_claim", []), "answer_contract.must_not_claim", issues),
    )


def _parse_cost_plan(raw: Any, issues: list[DispatcherValidationIssue]) -> DispatcherCostPlan:
    data = _mapping_or_issue(raw, "cost_plan", issues)
    return DispatcherCostPlan(
        max_planner_calls=_positive_int(data.get("max_planner_calls", 1), "cost_plan.max_planner_calls", issues, default=1),
        max_tool_actions=_positive_int(data.get("max_tool_actions", 4), "cost_plan.max_tool_actions", issues, default=4),
        max_ticket_ids=_positive_int(data.get("max_ticket_ids", 75), "cost_plan.max_ticket_ids", issues, default=75),
        max_rows=_positive_int(data.get("max_rows", 100), "cost_plan.max_rows", issues, default=100),
    )


def _coerce_mapping(
    payload: Mapping[str, Any] | str,
    field: str,
    issues: list[DispatcherValidationIssue],
) -> Mapping[str, Any]:
    if isinstance(payload, str):
        try:
            loaded = json.loads(payload)
        except json.JSONDecodeError:
            _add_issue(issues, field, "invalid_json", "Expected a JSON object.")
            return {}
        return _mapping_or_issue(loaded, field, issues)
    return _mapping_or_issue(payload, field, issues)


def _mapping_or_issue(
    raw: Any,
    field: str,
    issues: list[DispatcherValidationIssue],
) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    _add_issue(issues, field, "invalid_type", "Expected an object.")
    return {}


def _list_of_mappings(
    raw: Any,
    field: str,
    issues: list[DispatcherValidationIssue],
) -> tuple[Mapping[str, Any], ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        _add_issue(issues, field, "invalid_type", "Expected a list of objects.")
        return ()
    parsed: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            _add_issue(issues, f"{field}[{index}]", "invalid_type", "Expected an object.")
            continue
        parsed.append(item)
    return tuple(parsed)


def _enum_value(
    enum_type: type[Enum],
    raw: Any,
    field: str,
    issues: list[DispatcherValidationIssue],
) -> Any | None:
    cleaned = _clean_str(raw)
    if not cleaned:
        _add_issue(issues, field, "required", "A supported value is required.")
        return None
    try:
        return enum_type(cleaned)
    except ValueError:
        _add_issue(issues, field, "unsupported_value", "Unsupported value.")
        return None


def _confidence(raw: Any, field: str, issues: list[DispatcherValidationIssue]) -> float:
    if isinstance(raw, bool):
        _add_issue(issues, field, "invalid_type", "Expected a number from 0 to 1.")
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _add_issue(issues, field, "invalid_type", "Expected a number from 0 to 1.")
        return 0.0
    if value < 0.0 or value > 1.0:
        _add_issue(issues, field, "out_of_range", "Expected a number from 0 to 1.")
        return value
    return value


def _clean_str(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _clean_str_list(raw: Any, field: str, issues: list[DispatcherValidationIssue]) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        _add_issue(issues, field, "invalid_type", "Expected a list of strings.")
        return ()
    values: list[str] = []
    for index, item in enumerate(raw):
        if item is None or isinstance(item, (dict, list)):
            _add_issue(issues, f"{field}[{index}]", "invalid_type", "Expected a string.")
            continue
        cleaned = _clean_str(item)
        if cleaned:
            values.append(cleaned)
    return tuple(dict.fromkeys(values))


def _clean_store_ids(raw: Any, field: str, issues: list[DispatcherValidationIssue]) -> tuple[str, ...]:
    values = _clean_str_list(raw, field, issues)
    store_ids: list[str] = []
    for index, value in enumerate(values):
        if not value.isdigit():
            _add_issue(issues, f"{field}[{index}]", "invalid_store_id", "Expected a numeric store id.")
            continue
        store_ids.append(value)
    return tuple(dict.fromkeys(store_ids))


def _clean_ticket_ids(raw: Any, field: str, issues: list[DispatcherValidationIssue]) -> tuple[int, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        _add_issue(issues, field, "invalid_type", "Expected a list of ticket ids.")
        return ()
    values: list[int] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool):
            _add_issue(issues, f"{field}[{index}]", "invalid_type", "Expected an integer ticket id.")
            continue
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            _add_issue(issues, f"{field}[{index}]", "invalid_type", "Expected an integer ticket id.")
            continue
        if value <= 0:
            _add_issue(issues, f"{field}[{index}]", "out_of_range", "Expected a positive ticket id.")
            continue
        values.append(value)
    return tuple(dict.fromkeys(values))


def _optional_positive_int(raw: Any, field: str, issues: list[DispatcherValidationIssue]) -> int | None:
    if raw in (None, ""):
        return None
    return _positive_int(raw, field, issues, default=0)


def _positive_int(raw: Any, field: str, issues: list[DispatcherValidationIssue], *, default: int) -> int:
    if isinstance(raw, bool):
        _add_issue(issues, field, "invalid_type", "Expected a positive integer.")
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        _add_issue(issues, field, "invalid_type", "Expected a positive integer.")
        return default
    if value <= 0:
        _add_issue(issues, field, "out_of_range", "Expected a positive integer.")
        return default
    return value


def _add_issue(
    issues: list[DispatcherValidationIssue],
    field: str,
    code: str,
    message: str,
) -> None:
    issues.append(DispatcherValidationIssue(field=field, code=code, message=message))


__all__ = [
    "DispatcherAction",
    "DispatcherActionName",
    "DispatcherAnswerContract",
    "DispatcherCapDetail",
    "DispatcherCompileDecision",
    "DispatcherCompiledAction",
    "DispatcherComparisonContract",
    "DispatcherCostPlan",
    "DispatcherDeterministicEntities",
    "DispatcherFallback",
    "DispatcherMode",
    "DispatcherParseResult",
    "DispatcherPeriod",
    "DispatcherPolicyLimits",
    "DispatcherPlan",
    "DispatcherPriorResultRef",
    "DispatcherScope",
    "DispatcherSourceFamily",
    "DispatcherTaskType",
    "DispatcherValidationError",
    "DispatcherValidationIssue",
    "compile_dispatcher_payload",
    "compile_dispatcher_plan",
    "parse_dispatcher_plan",
    "parse_dispatcher_plan_or_raise",
]
