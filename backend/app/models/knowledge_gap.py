"""Data contracts for Knowledge Gaps v2.

These models describe the evidence-backed documentation intelligence workflow.
They are intentionally isolated from the current docs-gap stream endpoint until
the v2 analyzer and UI are wired in.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class AnalysisMode(str, Enum):
    TICKETS_VS_ALL = "tickets_vs_all"
    TICKETS_VS_ZENDESK = "tickets_vs_zendesk"
    TICKETS_VS_DRIVE = "tickets_vs_drive"
    SOURCE_VS_SOURCE = "source_vs_source"
    KNOWLEDGE_ONLY = "knowledge_only"


class ImpactMetric(str, Enum):
    MOST_FREQUENT = "most_frequent"
    SLOWEST_TO_RESOLVE = "slowest_to_resolve"
    MOST_ESCALATED = "most_escalated"
    MOST_REOPENED = "most_reopened"
    HIGHEST_TIME_COST = "highest_time_cost"
    FASTEST_GROWING = "fastest_growing"


class AnalysisDepth(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"


class KnowledgeSourceKind(str, Enum):
    ZENDESK_ARTICLES = "zendesk_articles"
    GDRIVE_SUPPORT_DOCS = "reference_support_docs"
    GDRIVE_OPERATIONS_JOB_AIDS = "gdrive_operations_job_aids"
    GDRIVE_SUPPORT_REPOSITORY = "gdrive_support_repository"


class GapType(str, Enum):
    MISSING_DOCUMENTATION = "missing_documentation"
    OUTDATED_DOCUMENTATION = "outdated_documentation"
    INCOMPLETE_DOCUMENTATION = "incomplete_documentation"
    HARD_TO_FIND = "hard_to_find"
    CONFLICTING_DOCUMENTATION = "conflicting_documentation"
    DUPLICATE_OR_STALE = "duplicate_or_stale"
    NOT_DOCUMENTATION_PROBLEM = "not_documentation_problem"


class Audience(str, Enum):
    ENGINEER = "engineer"
    STORE_OR_CUSTOMER = "store_or_customer"
    MANAGER_OR_OWNER = "manager_or_owner"


class RecommendedActionType(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    DELETE_OR_ARCHIVE = "delete_or_archive"
    RETITLE_OR_TAG = "retitle_or_tag"
    CROSS_LINK = "cross_link"
    OWNER_REVIEW = "owner_review"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    NEEDS_REVIEW = "needs_review"


class DraftType(str, Enum):
    NEW_ZENDESK_ARTICLE = "new_zendesk_article"
    ZENDESK_REWRITE = "zendesk_rewrite"
    GDRIVE_UPDATE_NOTE = "gdrive_update_note"
    CHECKLIST_SECTION = "checklist_section"
    MERGE_PLAN = "merge_plan"
    ARCHIVE_RATIONALE = "archive_rationale"
    OWNER_HANDOFF = "owner_handoff"
    TITLE_TAG_KEYWORDS = "title_tag_keywords"


class AnalysisScope(BaseModel):
    brands: list[str] = Field(default_factory=list)
    store_ids: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    systems_or_vendors: list[str] = Field(default_factory=list)
    issue_categories: list[str] = Field(default_factory=list)


class KnowledgeGapCriteria(BaseModel):
    mode: AnalysisMode = AnalysisMode.TICKETS_VS_ALL
    depth: AnalysisDepth = AnalysisDepth.QUICK
    pattern_limit: int = Field(default=3, ge=1, le=8)
    period_days: int = 90
    min_tickets: int = 25
    impact_metric: ImpactMetric = ImpactMetric.HIGHEST_TIME_COST
    sources: list[KnowledgeSourceKind] = Field(default_factory=list)
    gap_types: list[GapType] = Field(default_factory=list)
    audiences: list[Audience] = Field(default_factory=list)
    action_types: list[RecommendedActionType] = Field(default_factory=list)
    confidence: list[Confidence] = Field(default_factory=list)
    scope: AnalysisScope = Field(default_factory=AnalysisScope)


class TicketEvidence(BaseModel):
    ticket_id: str
    subject: str
    status: str | None = None
    brand: str | None = None
    store_id: str | None = None
    issue_category: str | None = None
    resolution_summary: str | None = None
    url: str | None = None


class TicketCluster(BaseModel):
    cluster_id: str
    title: str
    issue_category: str | None = None
    ticket_count: int
    avg_resolution_hours: float | None = None
    avg_open_days: float | None = None
    escalation_rate_pct: float | None = None
    reopen_rate_pct: float | None = None
    affected_brands: list[str] = Field(default_factory=list)
    affected_store_ids: list[str] = Field(default_factory=list)
    common_symptoms: list[str] = Field(default_factory=list)
    common_resolution_steps: list[str] = Field(default_factory=list)
    sample_tickets: list[TicketEvidence] = Field(default_factory=list)


class KnowledgeDocument(BaseModel):
    source: KnowledgeSourceKind
    title: str
    url: str
    document_id: str | None = None
    snippet: str | None = None
    matched_queries: list[str] = Field(default_factory=list)
    freshness: str | None = None
    owner: str | None = None


class SourceCoverage(BaseModel):
    source: KnowledgeSourceKind
    status: Literal["none", "weak", "partial", "good", "conflicting", "stale"]
    summary: str
    documents: list[KnowledgeDocument] = Field(default_factory=list)
    source_specific_issue: str | None = None


class MismatchStatement(BaseModel):
    docs_claim: str | None = None
    ticket_reality: str | None = None
    conclusion: str
    confidence: Confidence = Confidence.NEEDS_REVIEW


class RecommendedAction(BaseModel):
    action_type: RecommendedActionType
    source: KnowledgeSourceKind | None = None
    title: str
    rationale: str
    expected_impact: str | None = None
    owner_hint: str | None = None


class DraftOption(BaseModel):
    draft_type: DraftType
    label: str
    target_source: KnowledgeSourceKind | None = None
    description: str


class KnowledgeGapFinding(BaseModel):
    finding_id: str
    title: str
    summary: str
    gap_type: GapType
    confidence: Confidence
    impact_score: float | None = None
    impact_summary: str
    ticket_cluster: TicketCluster | None = None
    source_coverage: list[SourceCoverage] = Field(default_factory=list)
    mismatches: list[MismatchStatement] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    draft_options: list[DraftOption] = Field(default_factory=list)


class KnowledgeGapAnalysisResult(BaseModel):
    criteria: KnowledgeGapCriteria
    findings: list[KnowledgeGapFinding]
    notes: list[str] = Field(default_factory=list)
    run_id: str | None = None
    created_at: str | None = None


class DraftRequest(BaseModel):
    finding: KnowledgeGapFinding
    draft_type: DraftType
    audience: Audience = Audience.ENGINEER
    target_source: KnowledgeSourceKind | None = None
    instructions: str | None = None


class DraftSection(BaseModel):
    heading: str
    body: str
    basis: Literal["ticket_fact", "doc_fact", "recommendation", "open_question"]


class DraftResult(BaseModel):
    draft_type: DraftType
    title: str
    target_source: KnowledgeSourceKind | None = None
    sections: list[DraftSection]
    evidence_used: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    review_required: bool = True


class KnowledgeGapRunSummary(BaseModel):
    run_id: str
    title: str
    created_at: str
    period_days: int
    min_tickets: int
    mode: AnalysisMode
    sources: list[KnowledgeSourceKind] = Field(default_factory=list)
    finding_count: int
    top_findings: list[str] = Field(default_factory=list)


class KnowledgeGapRunRecord(BaseModel):
    run_id: str
    title: str
    created_at: str
    owner_uid: str | None = None
    owner_email: str | None = None
    result: KnowledgeGapAnalysisResult
