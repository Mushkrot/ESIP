export type AnalysisMode =
  | 'tickets_vs_all'
  | 'tickets_vs_zendesk'
  | 'tickets_vs_drive'
  | 'source_vs_source'
  | 'knowledge_only'

export type ImpactMetric =
  | 'most_frequent'
  | 'slowest_to_resolve'
  | 'most_escalated'
  | 'most_reopened'
  | 'highest_time_cost'
  | 'fastest_growing'

export type AnalysisDepth = 'quick' | 'standard'

export type KnowledgeSourceKind =
  | 'zendesk_articles'
  | 'reference_support_docs'
  | 'gdrive_operations_job_aids'
  | 'gdrive_support_repository'

export type GapType =
  | 'missing_documentation'
  | 'outdated_documentation'
  | 'incomplete_documentation'
  | 'hard_to_find'
  | 'conflicting_documentation'
  | 'duplicate_or_stale'
  | 'not_documentation_problem'

export type Audience =
  | 'engineer'
  | 'store_or_customer'
  | 'manager_or_owner'

export type RecommendedActionType =
  | 'create'
  | 'update'
  | 'merge'
  | 'delete_or_archive'
  | 'retitle_or_tag'
  | 'cross_link'
  | 'owner_review'

export type Confidence = 'high' | 'medium' | 'needs_review'

export type DraftType =
  | 'new_zendesk_article'
  | 'zendesk_rewrite'
  | 'gdrive_update_note'
  | 'checklist_section'
  | 'merge_plan'
  | 'archive_rationale'
  | 'owner_handoff'
  | 'title_tag_keywords'

export interface AnalysisScope {
  brands: string[]
  store_ids: string[]
  regions: string[]
  systems_or_vendors: string[]
  issue_categories: string[]
}

export interface KnowledgeGapCriteria {
  mode: AnalysisMode
  depth: AnalysisDepth
  pattern_limit: number
  period_days: number
  min_tickets: number
  impact_metric: ImpactMetric
  sources: KnowledgeSourceKind[]
  gap_types: GapType[]
  audiences: Audience[]
  action_types: RecommendedActionType[]
  confidence: Confidence[]
  scope: AnalysisScope
}

export interface TicketEvidence {
  ticket_id: string
  subject: string
  status?: string | null
  brand?: string | null
  store_id?: string | null
  issue_category?: string | null
  resolution_summary?: string | null
  url?: string | null
}

export interface TicketCluster {
  cluster_id: string
  title: string
  issue_category?: string | null
  ticket_count: number
  avg_resolution_hours?: number | null
  avg_open_days?: number | null
  escalation_rate_pct?: number | null
  reopen_rate_pct?: number | null
  affected_brands: string[]
  affected_store_ids: string[]
  common_symptoms: string[]
  common_resolution_steps: string[]
  sample_tickets: TicketEvidence[]
}

export interface KnowledgeDocument {
  source: KnowledgeSourceKind
  title: string
  url: string
  document_id?: string | null
  snippet?: string | null
  matched_queries: string[]
  freshness?: string | null
  owner?: string | null
}

export interface SourceCoverage {
  source: KnowledgeSourceKind
  status: 'none' | 'weak' | 'partial' | 'good' | 'conflicting' | 'stale'
  summary: string
  documents: KnowledgeDocument[]
  source_specific_issue?: string | null
}

export interface MismatchStatement {
  docs_claim?: string | null
  ticket_reality?: string | null
  conclusion: string
  confidence: Confidence
}

export interface RecommendedAction {
  action_type: RecommendedActionType
  source?: KnowledgeSourceKind | null
  title: string
  rationale: string
  expected_impact?: string | null
  owner_hint?: string | null
}

export interface DraftOption {
  draft_type: DraftType
  label: string
  target_source?: KnowledgeSourceKind | null
  description: string
}

export interface KnowledgeGapFinding {
  finding_id: string
  title: string
  summary: string
  gap_type: GapType
  confidence: Confidence
  impact_score?: number | null
  impact_summary: string
  ticket_cluster?: TicketCluster | null
  source_coverage: SourceCoverage[]
  mismatches: MismatchStatement[]
  recommended_actions: RecommendedAction[]
  draft_options: DraftOption[]
}

export interface KnowledgeGapAnalysisResult {
  criteria: KnowledgeGapCriteria
  findings: KnowledgeGapFinding[]
  notes: string[]
  run_id?: string | null
  created_at?: string | null
}

export interface DraftRequest {
  finding: KnowledgeGapFinding
  draft_type: DraftType
  audience: Audience
  target_source?: KnowledgeSourceKind | null
  instructions?: string | null
}

export interface DraftSection {
  heading: string
  body: string
  basis: 'ticket_fact' | 'doc_fact' | 'recommendation' | 'open_question'
}

export interface DraftResult {
  draft_type: DraftType
  title: string
  target_source?: KnowledgeSourceKind | null
  sections: DraftSection[]
  evidence_used: string[]
  open_questions: string[]
  review_required: boolean
}

export interface KnowledgeGapRunSummary {
  run_id: string
  title: string
  created_at: string
  period_days: number
  min_tickets: number
  mode: AnalysisMode
  sources: KnowledgeSourceKind[]
  finding_count: number
  top_findings: string[]
}

export interface KnowledgeGapRunRecord {
  run_id: string
  title: string
  created_at: string
  owner_uid?: string | null
  owner_email?: string | null
  result: KnowledgeGapAnalysisResult
}
