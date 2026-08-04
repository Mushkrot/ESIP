"""
Models for Universal Chat quality review traces and future review actions.

Phase 1 only captures answer traces. Review/rerun endpoints will build on these
contracts without changing the base Universal Chat stream behavior.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


ChatQualityLabel = Literal["good", "weak", "likely_wrong", "insufficient_evidence"]
ChatQualityProblem = Literal[
    "wrong_routing",
    "weak_sources",
    "obsolete_source",
    "conflicting_sources",
    "ambiguous_question",
    "ignored_negative_constraint",
    "answer_too_generic",
]
ChatQualityAction = Literal[
    "keep",
    "ask_clarification",
    "reroute",
    "run_semantic_plus_exact",
    "rewrite_with_existing_sources",
]

ChatValidatorQuality = Literal["good", "suspect", "unsafe"]
ChatValidatorRiskClass = Literal[
    "false_no_data",
    "tool_error_as_absence",
    "stale_data",
    "source_family_mismatch",
    "route_task_mismatch",
    "evidence_mismatch",
    "context_loss",
    "unsupported_strong_claim",
    "weak_evidence",
    "denominator_drift",
    "raw_artifact",
    "validator_error",
]
ChatValidatorAction = Literal[
    "none",
    "rerun_bq",
    "zendesk_fallback",
    "drive_exact_retry",
    "rebuild_scoped_evidence",
    "rewrite_with_existing_sources",
    "final_with_caveat",
]
ChatValidatorAnswerPolicy = Literal[
    "normal",
    "partial",
    "no_data_checked",
    "caveated",
]
ChatRepairLoopStopReason = Literal[
    "none",
    "accepted",
    "loop_disabled",
    "validator_disabled",
    "max_validations",
    "max_repairs",
    "repeated_action",
    "no_progress",
    "scope_drift",
    "repair_not_allowed",
    "repair_failed",
    "validator_failure",
    "answer_now",
    "answer_now_waiting_for_candidate",
    "escalation_disabled",
    "routine_answer",
    "escalation_cap",
    "escalation_not_allowlisted",
]


class ChatAnswerTrace(BaseModel):
    trace_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    ticket_id: int | None = None
    target_ticket_id: int | None = None
    ticket_context_reason: str = "none"
    phase1_decision: dict[str, Any] = Field(default_factory=dict)
    policy_flags: dict[str, Any] = Field(default_factory=dict)
    kb_results: list[dict[str, Any]] = Field(default_factory=list)
    kb_status: dict[str, Any] | None = None
    drive_chunks: list[dict[str, Any]] = Field(default_factory=list)
    similar_tickets: list[dict[str, Any]] = Field(default_factory=list)
    bq_sql: str = ""
    bq_rows: list[dict[str, Any]] | str | None = None
    final_answer: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    tickets: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ChatQualityReviewRequest(BaseModel):
    trace_id: str
    user_note: str | None = None


class ChatQualityReviewResult(BaseModel):
    quality: ChatQualityLabel
    confidence: float = Field(ge=0.0, le=1.0)
    problem_types: list[ChatQualityProblem] = Field(default_factory=list)
    recommended_action: ChatQualityAction
    reformulated_query: str | None = None
    explanation: str
    user_visible_note: str
    improved_answer: str | None = None


class ChatAnswerValidationResult(BaseModel):
    """Internal automatic validator result for a completed answer candidate."""

    quality: ChatValidatorQuality
    confidence: float = Field(ge=0.0, le=1.0)
    risk_classes: list[ChatValidatorRiskClass] = Field(default_factory=list)
    recommended_action: ChatValidatorAction
    answer_policy: ChatValidatorAnswerPolicy
    reason_short: str
    user_status: str | None = None
    rewrite_from_supplied_evidence: str | None = None
    validator_enabled: bool = True


class ChatAnswerRiskDetectionResult(BaseModel):
    """Deterministic pre-validator risk decision for one answer candidate."""

    should_validate: bool
    risk_classes: list[ChatValidatorRiskClass] = Field(default_factory=list)
    risk_reasons: dict[str, str] = Field(default_factory=dict)
    source_families: list[str] = Field(default_factory=list)


class ChatRepairScopeFingerprint(BaseModel):
    """Original user/request scope that automatic repair must not silently widen."""

    latest_question: str | None = None
    target_ticket_id: str | None = None
    store_id: str | None = None
    pos_type: str | None = None
    status_filter: tuple[str, ...] = Field(default_factory=tuple)
    date_start: str | None = None
    date_end: str | None = None
    source_family: str | None = None
    denominator_key: str | None = None


class ChatRepairLoopIteration(BaseModel):
    """Compact private telemetry for one validation/repair loop iteration."""

    iteration: int
    validation_count: int
    repair_count: int
    validator_quality: ChatValidatorQuality | None = None
    validator_action: ChatValidatorAction | None = None
    repair_signature: str | None = None
    repair_attempted: bool = False
    repair_applied: bool = False
    stop_reason: ChatRepairLoopStopReason = "none"


class ChatRepairLoopState(BaseModel):
    """Pure controller state for bounded automatic answer repair."""

    enabled: bool = True
    max_repairs: int = 2
    max_validations: int = 3
    validation_count: int = 0
    repair_count: int = 0
    repair_signatures: list[str] = Field(default_factory=list)
    scope_fingerprint: ChatRepairScopeFingerprint | None = None
    stop_reason: ChatRepairLoopStopReason = "none"
    answer_now_requested: bool = False
    answer_now_checkpoint: str | None = None
    answer_now_final_policy: str | None = None


class ChatRepairLoopDecision(BaseModel):
    """Allow/stop decision from the pure repair-loop policy layer."""

    allowed: bool
    stop_reason: ChatRepairLoopStopReason = "none"
    reason: str = ""
