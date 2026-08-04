"""Structured Knowledge Gaps v2 analyzer.

This module stays inside the Knowledge Gaps product boundary. It uses read-only
ticket analytics plus existing KB/Drive retrieval services to produce structured
findings and reviewable documentation draft scaffolds.
"""

from __future__ import annotations

import re

from app.models.knowledge_gap import (
    AnalysisDepth,
    Audience,
    Confidence,
    DraftOption,
    DraftRequest,
    DraftResult,
    DraftSection,
    DraftType,
    GapType,
    KnowledgeDocument,
    KnowledgeGapAnalysisResult,
    KnowledgeGapCriteria,
    KnowledgeGapFinding,
    KnowledgeSourceKind,
    MismatchStatement,
    RecommendedAction,
    RecommendedActionType,
    SourceCoverage,
    TicketCluster,
    TicketEvidence,
)
from app.services import docs_gap, vertex_search
from app.services import bigquery as bq_service


ZENDESK_VERTEX_SOURCES = ["zendesk_support_articles", "zendesk_operations_articles"]

SOURCE_SEARCH_IDS: dict[KnowledgeSourceKind, list[str]] = {
    KnowledgeSourceKind.ZENDESK_ARTICLES: ZENDESK_VERTEX_SOURCES,
    KnowledgeSourceKind.GDRIVE_SUPPORT_DOCS: ["reference_support_docs"],
    KnowledgeSourceKind.GDRIVE_OPERATIONS_JOB_AIDS: ["gdrive_operations_job_aids"],
    KnowledgeSourceKind.GDRIVE_SUPPORT_REPOSITORY: ["gdrive_support_repository"],
}


DEFAULT_SOURCES = [
    KnowledgeSourceKind.ZENDESK_ARTICLES,
    KnowledgeSourceKind.GDRIVE_SUPPORT_DOCS,
    KnowledgeSourceKind.GDRIVE_OPERATIONS_JOB_AIDS,
    KnowledgeSourceKind.GDRIVE_SUPPORT_REPOSITORY,
]

DEPTH_CATEGORY_LIMITS = {
    AnalysisDepth.QUICK: 3,
    AnalysisDepth.STANDARD: 4,
}


def analyze(criteria: KnowledgeGapCriteria) -> KnowledgeGapAnalysisResult:
    """Return structured findings from ticket patterns and source coverage."""
    sources = _sources_for_criteria(criteria)
    normalized_criteria = criteria.model_copy(update={"sources": sources})

    categories = docs_gap.fetch_top_categories(
        days=criteria.period_days,
        min_tickets=criteria.min_tickets,
        sort_by=_sort_by_from_impact(criteria),
    )

    findings: list[KnowledgeGapFinding] = []
    category_limit = _category_limit(criteria)
    for category in categories[:category_limit]:
        cluster = _build_ticket_cluster(category, criteria.period_days)
        query = _build_coverage_query(category, cluster)
        coverages = [
            _build_source_coverage(query, category["display_name"], source)
            for source in sources
        ]
        has_any_docs = any(c.documents for c in coverages)
        gap_type = _classify_gap_type(coverages, cluster)
        ticket_count = int(category.get("ticket_count") or 0)
        avg_resolution = category.get("avg_resolution_hours")
        impact_score = ticket_count * float(avg_resolution or 0)
        finding_id = _slug_id(category["issue_category"])

        finding = KnowledgeGapFinding(
            finding_id=finding_id,
            title=f"Review documentation for {category['display_name']}",
            summary=_summary(category, coverages),
            gap_type=gap_type,
            confidence=_confidence(coverages, cluster),
            impact_score=impact_score,
            impact_summary=(
                f"{ticket_count} tickets in {criteria.period_days} days; "
                f"avg resolution {avg_resolution}h; escalated {category.get('pct_escalated')}%"
            ),
            ticket_cluster=cluster,
            source_coverage=coverages,
            mismatches=_mismatches(category, cluster, coverages),
            recommended_actions=_recommended_actions(category["display_name"], coverages, cluster),
            draft_options=_draft_options(coverages),
        )
        if _finding_matches_filters(finding, normalized_criteria):
            findings.append(finding)

    return KnowledgeGapAnalysisResult(
        criteria=normalized_criteria,
        findings=findings,
        notes=[
            "v2 analyzes real ticket samples, source-specific coverage, and reviewable source actions.",
            f"Interactive analysis checked the top {category_limit} ticket patterns.",
            "Existing /api/docs-gap/stream markdown report remains unchanged.",
        ],
    )


def draft(req: DraftRequest) -> DraftResult:
    """Create a reviewable draft scaffold from an existing finding.

    This first drafting slice is deterministic on purpose. It separates ticket
    facts, document facts, recommendations, and open questions without inventing
    a final procedure.
    """
    finding = req.finding
    target = req.target_source or _default_target_source(finding)
    title = _draft_title(finding, req.draft_type, target)
    cluster = finding.ticket_cluster

    sections: list[DraftSection] = []
    if cluster:
        sections.append(
            DraftSection(
                heading="Ticket Evidence",
                basis="ticket_fact",
                body=_ticket_evidence_body(cluster),
            )
        )

    doc_body = _document_evidence_body(finding, target)
    if doc_body:
        sections.append(
            DraftSection(
                heading="Current Documentation Evidence",
                basis="doc_fact",
                body=doc_body,
            )
        )

    sections.append(
        DraftSection(
            heading=_recommendation_heading(req.draft_type),
            basis="recommendation",
            body=_recommendation_body(finding, req.draft_type, req.audience, target),
        )
    )

    open_questions = _open_questions(finding, target)
    sections.append(
        DraftSection(
            heading="Human Review Required",
            basis="open_question",
            body="\n".join(f"- {q}" for q in open_questions),
        )
    )

    evidence_used = []
    if cluster:
        evidence_used.extend(f"Ticket #{ticket.ticket_id}" for ticket in cluster.sample_tickets)
    for coverage in finding.source_coverage:
        for doc in coverage.documents[:3]:
            evidence_used.append(f"{coverage.source.value}: {doc.title}")

    return DraftResult(
        draft_type=req.draft_type,
        title=title,
        target_source=target,
        sections=sections,
        evidence_used=evidence_used,
        open_questions=open_questions,
        review_required=True,
    )


def _sort_by_from_impact(criteria: KnowledgeGapCriteria) -> str:
    if criteria.impact_metric.value == "slowest_to_resolve":
        return "resolution_time"
    if criteria.impact_metric.value in {"highest_time_cost", "most_frequent"}:
        return "volume"
    return "resolution_time"


def _category_limit(criteria: KnowledgeGapCriteria) -> int:
    return max(1, min(8, int(criteria.pattern_limit or DEPTH_CATEGORY_LIMITS[criteria.depth])))


def _sources_for_criteria(criteria: KnowledgeGapCriteria) -> list[KnowledgeSourceKind]:
    if criteria.sources:
        return criteria.sources
    if criteria.mode.value == "tickets_vs_zendesk":
        return [KnowledgeSourceKind.ZENDESK_ARTICLES]
    if criteria.mode.value == "tickets_vs_drive":
        return [
            KnowledgeSourceKind.GDRIVE_SUPPORT_DOCS,
            KnowledgeSourceKind.GDRIVE_OPERATIONS_JOB_AIDS,
        ]
    return DEFAULT_SOURCES


def _build_ticket_cluster(category: dict, days: int) -> TicketCluster:
    issue_category = category["issue_category"]
    samples = _fetch_sample_tickets(issue_category, days)
    fallback_subjects = category.get("sample_subjects", [])[:3]
    sample_tickets = samples or [
        TicketEvidence(
            ticket_id="sample",
            subject=subject,
            issue_category=issue_category,
        )
        for subject in fallback_subjects
    ]
    common_symptoms = _dedupe_keep_order(
        [ticket.subject for ticket in sample_tickets if ticket.subject]
        + list(fallback_subjects)
    )[:5]
    common_resolution_steps = _extract_resolution_steps(sample_tickets)
    affected_brands = _dedupe_keep_order(
        [ticket.brand or "" for ticket in sample_tickets if ticket.brand]
    )[:6]
    affected_stores = _dedupe_keep_order(
        [ticket.store_id or "" for ticket in sample_tickets if ticket.store_id]
    )[:8]

    return TicketCluster(
        cluster_id=_slug_id(issue_category),
        title=category["display_name"],
        issue_category=issue_category,
        ticket_count=int(category.get("ticket_count") or 0),
        avg_resolution_hours=category.get("avg_resolution_hours"),
        avg_open_days=category.get("avg_open_days"),
        escalation_rate_pct=category.get("pct_escalated"),
        affected_brands=affected_brands,
        affected_store_ids=affected_stores,
        common_symptoms=common_symptoms,
        common_resolution_steps=common_resolution_steps,
        sample_tickets=sample_tickets,
    )


def _fetch_sample_tickets(issue_category: str, days: int) -> list[TicketEvidence]:
    """Fetch concrete ticket examples for a category from BigQuery."""
    from google.cloud import bigquery as bq

    client = bq_service._get_client()
    sql = f"""
    SELECT id, subject, status, brand, store_id, issue_category, resolution
    FROM `{bq_service.GCP_PROJECT}.{bq_service.BQ_DATASET}.tickets`
    WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
      AND issue_category = @issue_category
    ORDER BY
      CASE WHEN resolution IS NOT NULL AND TRIM(resolution) != '' THEN 0 ELSE 1 END,
      solved_at DESC,
      created_at DESC
    LIMIT 5
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("days", "INT64", days),
            bq.ScalarQueryParameter("issue_category", "STRING", issue_category),
        ]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except Exception:
        return []

    tickets: list[TicketEvidence] = []
    for row in rows:
        ticket_id = str(row.id)
        tickets.append(
            TicketEvidence(
                ticket_id=ticket_id,
                subject=row.subject or "",
                status=row.status,
                brand=row.brand,
                store_id=row.store_id,
                issue_category=row.issue_category,
                resolution_summary=_compact_text(row.resolution or "", 360) or None,
                url=f"https://support.example.com/agent/tickets/{ticket_id}",
            )
        )
    return tickets


def _build_coverage_query(category: dict, cluster: TicketCluster) -> str:
    parts = [category["display_name"], category["issue_category"].replace("__", " ")]
    parts.extend(cluster.common_symptoms[:2])
    parts.extend(cluster.common_resolution_steps[:2])
    return " ".join(_compact_text(part, 90) for part in parts if part).strip()


def _build_source_coverage(query: str, category_title: str, source: KnowledgeSourceKind) -> SourceCoverage:
    documents: list[KnowledgeDocument] = []
    if source == KnowledgeSourceKind.ZENDESK_ARTICLES:
        results = vertex_search.search_kb(
            query or category_title,
            page_size=3,
            sources=SOURCE_SEARCH_IDS[source],
        )
        documents.extend([
            KnowledgeDocument(
                source=source,
                title=result.title,
                url=result.url,
                snippet=result.snippet,
                matched_queries=[query or category_title],
            )
            for result in results
        ])
    else:
        # Drive coverage uses the BQ exact/ranked index. Running Vertex semantic
        # Drive search for every category made the interactive endpoint exceed
        # Firebase Hosting proxy timeouts; deeper semantic Drive comparison
        # belongs in persisted/background runs.
        documents.extend(_drive_exact_documents(query or category_title, source))
    documents = _dedupe_documents(documents)[:5]
    status = _coverage_status(documents, query)
    summary = (
        f"{len(documents)} candidate document(s) found for {category_title}."
        if documents
        else f"No candidate documents found for {category_title}."
    )
    return SourceCoverage(
        source=source,
        status=status,
        summary=summary,
        documents=documents,
        source_specific_issue=(
            "Candidate coverage needs resolution-step validation against tickets."
            if documents
            else "Potential missing coverage in this source."
        ),
    )


def _drive_exact_documents(query: str, source: KnowledgeSourceKind) -> list[KnowledgeDocument]:
    rows = bq_service.search_drive_exact(
        query=query,
        page_size=3,
        source_ids=SOURCE_SEARCH_IDS[source],
    )
    docs: list[KnowledgeDocument] = []
    for row in rows:
        docs.append(
            KnowledgeDocument(
                source=source,
                title=row.get("file_name") or "Drive document",
                url=row.get("drive_url") or "",
                snippet=_compact_text(row.get("chunk_text") or "", 260) or None,
                matched_queries=[query],
                freshness=row.get("source_path"),
            )
        )
    return [doc for doc in docs if doc.url]


def _summary(category: dict, coverages: list[SourceCoverage]) -> str:
    statuses = ", ".join(f"{c.source.value}: {c.status}" for c in coverages)
    return (
        f"{category['display_name']} generated {category['ticket_count']} tickets "
        f"with average resolution {category['avg_resolution_hours']}h. Source coverage: {statuses}."
    )


def _recommended_actions(
    category_title: str,
    coverages: list[SourceCoverage],
    cluster: TicketCluster,
) -> list[RecommendedAction]:
    actions: list[RecommendedAction] = []
    for coverage in coverages:
        if coverage.status in {"good", "partial", "weak"}:
            actions.append(
                RecommendedAction(
                    action_type=RecommendedActionType.UPDATE,
                    source=coverage.source,
                    title=f"Validate and update {coverage.source.value} coverage for {category_title}",
                    rationale=(
                        "Candidate documents exist, but they need comparison against "
                        "actual successful ticket resolutions and engineer workarounds."
                    ),
                    expected_impact="Reduce repeat tickets or shorten resolution time if the documented process is incomplete or stale.",
                )
            )
        elif coverage.status == "stale":
            actions.append(
                RecommendedAction(
                    action_type=RecommendedActionType.DELETE_OR_ARCHIVE,
                    source=coverage.source,
                    title=f"Review stale {coverage.source.value} coverage for {category_title}",
                    rationale="Search returned content that appears obsolete or marked no longer valid.",
                    expected_impact="Prevent engineers from following stale guidance and improve search precision.",
                )
            )
        else:
            actions.append(
                RecommendedAction(
                    action_type=RecommendedActionType.CREATE,
                    source=coverage.source,
                    title=f"Create source-specific documentation for {category_title}",
                    rationale="No candidate document was found in this source for a recurring ticket category.",
                    expected_impact="Give engineers a findable reference before similar tickets are opened or escalated.",
                )
            )
    if cluster.common_resolution_steps:
        actions.append(
            RecommendedAction(
                action_type=RecommendedActionType.CROSS_LINK,
                source=None,
                title="Cross-link the final procedure from every related source",
                rationale="Tickets show repeat resolution patterns; engineers should not have to choose between disconnected Zendesk and Drive references.",
                expected_impact="Reduce search time and inconsistent ticket handling.",
            )
        )
    return actions


def _draft_options(coverages: list[SourceCoverage]) -> list[DraftOption]:
    options = [
        DraftOption(
            draft_type=DraftType.OWNER_HANDOFF,
            label="Create owner handoff",
            description="Summarize the evidence and recommended source-specific actions for a document owner.",
        ),
        DraftOption(
            draft_type=DraftType.CHECKLIST_SECTION,
            label="Draft troubleshooting checklist",
            description="Draft a reviewable checklist from confirmed ticket and document evidence.",
        ),
    ]
    if any(c.source == KnowledgeSourceKind.ZENDESK_ARTICLES for c in coverages):
        options.append(
            DraftOption(
                draft_type=DraftType.ZENDESK_REWRITE,
                label="Draft Zendesk article update",
                target_source=KnowledgeSourceKind.ZENDESK_ARTICLES,
                description="Prepare a reviewable Zendesk Article update for this finding.",
            )
        )
    if any(c.source != KnowledgeSourceKind.ZENDESK_ARTICLES for c in coverages):
        options.append(
            DraftOption(
                draft_type=DraftType.GDRIVE_UPDATE_NOTE,
                label="Draft Drive update note",
                description="Prepare a reviewable note for updating the relevant Drive document or job aid.",
            )
        )
    return options


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "knowledge-gap"


def _classify_gap_type(coverages: list[SourceCoverage], cluster: TicketCluster) -> GapType:
    if not any(c.documents for c in coverages):
        return GapType.MISSING_DOCUMENTATION
    if any(c.status == "stale" for c in coverages):
        return GapType.OUTDATED_DOCUMENTATION
    if any(c.status == "none" for c in coverages) and any(c.documents for c in coverages):
        return GapType.INCOMPLETE_DOCUMENTATION
    if any(c.status == "weak" for c in coverages):
        return GapType.HARD_TO_FIND
    if cluster.common_resolution_steps and any(c.status == "partial" for c in coverages):
        return GapType.INCOMPLETE_DOCUMENTATION
    return GapType.NOT_DOCUMENTATION_PROBLEM


def _confidence(coverages: list[SourceCoverage], cluster: TicketCluster) -> Confidence:
    if cluster.sample_tickets and len(cluster.sample_tickets) >= 3 and any(c.status == "none" for c in coverages):
        return Confidence.HIGH
    if cluster.sample_tickets and any(c.documents for c in coverages):
        return Confidence.MEDIUM
    return Confidence.NEEDS_REVIEW


def _mismatches(
    category: dict,
    cluster: TicketCluster,
    coverages: list[SourceCoverage],
) -> list[MismatchStatement]:
    docs_claim = None
    if any(c.documents for c in coverages):
        docs_claim = "Candidate documents exist, but coverage is uneven across the selected sources."
    ticket_reality = (
        f"Tickets continue to recur under {category['display_name']} "
        f"({category['ticket_count']} tickets, avg resolution {category['avg_resolution_hours']}h)."
    )
    if cluster.common_resolution_steps:
        ticket_reality += " Sample resolutions mention: " + "; ".join(cluster.common_resolution_steps[:3])
    if not any(c.documents for c in coverages):
        conclusion = "The selected sources appear to lack findable coverage for a recurring ticket pattern."
    elif any(c.status == "none" for c in coverages):
        conclusion = "Coverage exists in some sources but not others, so engineers may receive inconsistent guidance depending on where they search."
    else:
        conclusion = "Candidate coverage exists, but the content should be validated against successful ticket resolutions before marking this as solved."
    return [
        MismatchStatement(
            docs_claim=docs_claim,
            ticket_reality=ticket_reality,
            conclusion=conclusion,
            confidence=_confidence(coverages, cluster),
        )
    ]


def _coverage_status(documents: list[KnowledgeDocument], query: str) -> str:
    if not documents:
        return "none"
    text = " ".join(f"{doc.title} {doc.snippet or ''}" for doc in documents).lower()
    if any(marker in text for marker in ("no longer valid", "obsolete", "deprecated", "retired")):
        return "stale"
    query_tokens = set(_tokens(query))
    doc_tokens = set(_tokens(text))
    overlap = len(query_tokens & doc_tokens)
    if overlap <= 1:
        return "weak"
    if len(documents) >= 3 and overlap >= 3:
        return "good"
    return "partial"


def _finding_matches_filters(finding: KnowledgeGapFinding, criteria: KnowledgeGapCriteria) -> bool:
    if criteria.gap_types and finding.gap_type not in criteria.gap_types:
        return False
    if criteria.confidence and finding.confidence not in criteria.confidence:
        return False
    if criteria.action_types:
        action_types = {action.action_type for action in finding.recommended_actions}
        if not any(action_type in action_types for action_type in criteria.action_types):
            return False
    scope = criteria.scope
    cluster = finding.ticket_cluster
    if scope.issue_categories and cluster and cluster.issue_category not in scope.issue_categories:
        return False
    if scope.brands and cluster:
        wanted = {b.lower() for b in scope.brands}
        if not any((brand or "").lower() in wanted for brand in cluster.affected_brands):
            return False
    if scope.store_ids and cluster:
        wanted = set(scope.store_ids)
        if not any(store in wanted for store in cluster.affected_store_ids):
            return False
    return True


def _dedupe_documents(documents: list[KnowledgeDocument]) -> list[KnowledgeDocument]:
    seen: set[tuple[str, str]] = set()
    out: list[KnowledgeDocument] = []
    for doc in documents:
        key = (doc.source.value, doc.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(doc)
    return out


def _extract_resolution_steps(sample_tickets: list[TicketEvidence]) -> list[str]:
    steps: list[str] = []
    for ticket in sample_tickets:
        if not ticket.resolution_summary:
            continue
        text = re.sub(r"\s+", " ", ticket.resolution_summary).strip()
        parts = re.split(r"(?<=[.!?])\s+|;\s+|\n+", text)
        for part in parts:
            compact = _compact_text(part, 180)
            if compact and len(compact.split()) >= 4:
                steps.append(compact)
    return _dedupe_keep_order(steps)[:5]


def _ticket_evidence_body(cluster: TicketCluster) -> str:
    lines = [
        f"{cluster.ticket_count} tickets; avg resolution {cluster.avg_resolution_hours}h; avg open {cluster.avg_open_days}d; escalation {cluster.escalation_rate_pct}%.",
    ]
    if cluster.common_symptoms:
        lines.append("Common signals: " + "; ".join(cluster.common_symptoms[:5]))
    if cluster.common_resolution_steps:
        lines.append("Observed resolution notes: " + "; ".join(cluster.common_resolution_steps[:5]))
    if cluster.sample_tickets:
        lines.append("Sample tickets:")
        lines.extend(
            f"- #{ticket.ticket_id}: {ticket.subject}"
            + (f" — {ticket.resolution_summary}" if ticket.resolution_summary else "")
            for ticket in cluster.sample_tickets[:5]
        )
    return "\n".join(lines)


def _document_evidence_body(finding: KnowledgeGapFinding, target: KnowledgeSourceKind | None) -> str:
    lines: list[str] = []
    for coverage in finding.source_coverage:
        if target and coverage.source != target:
            continue
        lines.append(f"{coverage.source.value}: {coverage.status}. {coverage.summary}")
        for doc in coverage.documents[:4]:
            lines.append(f"- {doc.title}: {doc.snippet or doc.url}")
    return "\n".join(lines)


def _recommendation_body(
    finding: KnowledgeGapFinding,
    draft_type: DraftType,
    audience: Audience,
    target: KnowledgeSourceKind | None,
) -> str:
    relevant_actions = [
        action for action in finding.recommended_actions
        if target is None or action.source is None or action.source == target
    ]
    action_lines = [
        f"- {action.title}: {action.rationale}"
        for action in relevant_actions[:6]
    ]
    prefix = {
        DraftType.NEW_ZENDESK_ARTICLE: "Create a new engineer-facing Zendesk article using only confirmed evidence below.",
        DraftType.ZENDESK_REWRITE: "Rewrite the existing Zendesk guidance after validating the procedure owner signs off.",
        DraftType.GDRIVE_UPDATE_NOTE: "Prepare a Google Drive document update note for the document owner.",
        DraftType.CHECKLIST_SECTION: "Draft a checklist section that an engineer can follow after the open questions are answered.",
        DraftType.MERGE_PLAN: "Plan how duplicate or overlapping references should be merged.",
        DraftType.ARCHIVE_RATIONALE: "Explain why stale or misleading references should be archived.",
        DraftType.OWNER_HANDOFF: "Send this handoff to the owner responsible for closing the documentation gap.",
        DraftType.TITLE_TAG_KEYWORDS: "Improve title, tags, and search terms so engineers can find the right reference.",
    }.get(draft_type, "Prepare a reviewable documentation update.")
    body = f"{prefix}\nAudience: {audience.value}."
    if action_lines:
        body += "\nRecommended actions:\n" + "\n".join(action_lines)
    return body


def _open_questions(finding: KnowledgeGapFinding, target: KnowledgeSourceKind | None) -> list[str]:
    questions = [
        "Which observed ticket resolution steps are approved as the official procedure?",
        "Who owns the target document or Zendesk article?",
    ]
    if finding.gap_type == GapType.MISSING_DOCUMENTATION:
        questions.append("Should this become a new Zendesk article, a Drive job aid, or both?")
    if target == KnowledgeSourceKind.GDRIVE_OPERATIONS_JOB_AIDS:
        questions.append("Should the Job Aid be updated in place, retitled, or cross-linked from Zendesk?")
    return questions


def _default_target_source(finding: KnowledgeGapFinding) -> KnowledgeSourceKind | None:
    for action in finding.recommended_actions:
        if action.source is not None:
            return action.source
    return None


def _draft_title(
    finding: KnowledgeGapFinding,
    draft_type: DraftType,
    target: KnowledgeSourceKind | None,
) -> str:
    target_text = target.value if target else "all sources"
    return f"{draft_type.value.replace('_', ' ').title()}: {finding.title} ({target_text})"


def _recommendation_heading(draft_type: DraftType) -> str:
    if draft_type in {DraftType.NEW_ZENDESK_ARTICLE, DraftType.ZENDESK_REWRITE, DraftType.CHECKLIST_SECTION}:
        return "Draft Content Direction"
    if draft_type in {DraftType.MERGE_PLAN, DraftType.ARCHIVE_RATIONALE}:
        return "Document Governance Direction"
    return "Recommended Handoff"


def _compact_text(value: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def _tokens(value: str) -> list[str]:
    stop = {"the", "and", "for", "with", "from", "this", "that", "into", "ticket", "tickets"}
    return [
        token for token in re.findall(r"[a-z0-9]{3,}", value.lower())
        if token not in stop
    ]


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value or "").strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out
