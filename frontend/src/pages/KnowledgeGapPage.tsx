import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  BookOpen,
  FileSearch,
  Loader2,
  RotateCcw,
  Send,
  Sparkles,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from '@/components/ui/resizable'
import { authHeaders } from '@/lib/api'
import type {
  Audience,
  AnalysisDepth,
  AnalysisMode,
  Confidence,
  DraftResult,
  DraftType,
  GapType,
  ImpactMetric,
  KnowledgeGapAnalysisResult,
  KnowledgeGapFinding,
  KnowledgeGapRunRecord,
  KnowledgeGapRunSummary,
  KnowledgeSourceKind,
  RecommendedActionType,
} from '@/types/knowledgeGap'

interface LegacyMessage {
  role: 'user' | 'assistant'
  content: string
}

const SOURCE_LABELS: Record<KnowledgeSourceKind, string> = {
  zendesk_articles: 'Zendesk Articles',
  reference_support_docs: 'Drive Support Docs',
  gdrive_operations_job_aids: 'Drive Job Aids',
  gdrive_support_repository: 'Support Repository',
}

const GAP_LABELS: Record<string, string> = {
  missing_documentation: 'Missing docs',
  outdated_documentation: 'Outdated docs',
  incomplete_documentation: 'Incomplete docs',
  hard_to_find: 'Hard to find',
  conflicting_documentation: 'Conflicting docs',
  duplicate_or_stale: 'Duplicate/stale',
  not_documentation_problem: 'Not a docs issue',
}

const CONFIDENCE_LABELS: Record<string, string> = {
  high: 'High',
  medium: 'Medium',
  needs_review: 'Needs review',
}

const COVERAGE_LABELS: Record<string, string> = {
  none: 'No coverage',
  weak: 'Weak',
  partial: 'Partial',
  good: 'Good',
  conflicting: 'Conflicting',
  stale: 'Stale',
}

const ACTION_LABELS: Record<string, string> = {
  create: 'Create new doc',
  update: 'Update existing doc',
  merge: 'Merge overlapping docs',
  delete_or_archive: 'Archive stale doc',
  retitle_or_tag: 'Improve title/tags',
  cross_link: 'Add cross-links',
  owner_review: 'Needs owner review',
}

const AUDIENCE_LABELS: Record<Audience, string> = {
  engineer: 'Engineer',
  store_or_customer: 'Store/customer',
  manager_or_owner: 'Manager/owner',
}

const DRAFT_BASIS_LABELS: Record<string, string> = {
  ticket_fact: 'Ticket fact',
  doc_fact: 'Doc fact',
  recommendation: 'Recommendation',
  open_question: 'Open question',
}

const MODE_SUMMARIES: Record<AnalysisMode, string> = {
  tickets_vs_all: 'Use tickets as the starting point, then check whether the selected knowledge sources explain how engineers should solve those issues.',
  tickets_vs_zendesk: 'Use tickets as the starting point, then check whether Zendesk Articles give engineers the right guidance.',
  tickets_vs_drive: 'Use tickets as the starting point, then check whether Drive documents and job aids match how issues are actually resolved.',
  source_vs_source: 'Check whether the selected knowledge sources agree with each other or whether one source appears stale, incomplete, or missing.',
  knowledge_only: 'Review the selected knowledge sources themselves, without using ticket volume as the main signal.',
}

const IMPACT_SUMMARIES: Record<ImpactMetric, string> = {
  highest_time_cost: 'bring the biggest time-saving opportunities to the top',
  most_frequent: 'bring the most repeated ticket patterns to the top',
  slowest_to_resolve: 'bring the slowest ticket patterns to the top',
  most_escalated: 'bring issues that often need escalation to the top',
  most_reopened: 'bring issues that often come back after closure to the top',
  fastest_growing: 'bring newly growing ticket patterns to the top',
}

const SOURCE_SELECTION_LABELS: Record<string, string> = {
  all: 'Zendesk Articles, Drive Support Docs, Drive Job Aids, and Support Repository',
  zendesk: 'Zendesk Articles only',
  drive: 'Google Drive sources only',
  support_docs: 'Drive Support Docs only',
  job_aids: 'Drive Job Aids only',
  support_repository: 'Support Repository only',
}

const PATTERN_LIMIT_MIN = 1
const PATTERN_LIMIT_MAX = 8

const GAP_FILTER_SUMMARIES: Record<GapType, string> = {
  missing_documentation: 'where engineers appear to lack a usable reference',
  outdated_documentation: 'where the existing guidance may no longer match reality',
  incomplete_documentation: 'where a document exists but likely does not cover enough of the real resolution path',
  hard_to_find: 'where guidance may exist but is not easy enough to find',
  conflicting_documentation: 'where sources may be telling engineers different things',
  duplicate_or_stale: 'where duplicate or stale references may be creating noise',
  not_documentation_problem: 'where documentation may not be the main cause',
}

const CONFIDENCE_FILTER_SUMMARIES: Record<Confidence, string> = {
  high: 'only when the evidence is strong enough to act on first',
  medium: 'including likely findings that still need owner review',
  needs_review: 'only where the system is unsure and wants a human check',
}

const ACTION_FILTER_SUMMARIES: Record<RecommendedActionType, string> = {
  create: 'where a new reference is likely needed',
  update: 'where an existing reference likely needs to be corrected or expanded',
  merge: 'where overlapping references may need to become one clearer source',
  delete_or_archive: 'where stale material may need to be removed from daily use',
  retitle_or_tag: 'where the content may be right but hard to discover',
  cross_link: 'where engineers need links between related sources',
  owner_review: 'where the next step is a decision from the document owner',
}

function sourceSelectionToList(value: string): KnowledgeSourceKind[] {
  if (value === 'zendesk') return ['zendesk_articles']
  if (value === 'drive') return ['reference_support_docs', 'gdrive_operations_job_aids', 'gdrive_support_repository']
  if (value === 'support_docs') return ['reference_support_docs']
  if (value === 'job_aids') return ['gdrive_operations_job_aids']
  if (value === 'support_repository') return ['gdrive_support_repository']
  return ['zendesk_articles', 'reference_support_docs', 'gdrive_operations_job_aids', 'gdrive_support_repository']
}

function clampPatternLimit(value: string): number {
  const parsed = Number.parseInt(value, 10)
  if (Number.isNaN(parsed)) return 3
  return Math.min(PATTERN_LIMIT_MAX, Math.max(PATTERN_LIMIT_MIN, parsed))
}

function patternLimitSummary(value: number): string {
  if (value <= 2) return 'Fewer patterns finishes faster, but may miss useful documentation gaps.'
  if (value <= 4) return 'More patterns gives broader coverage while keeping the result easy to review.'
  return 'Many patterns can take longer and add noise; use it when you want a wider pass.'
}

function impactToLegacySort(value: ImpactMetric): string {
  if (value === 'most_frequent' || value === 'highest_time_cost') return 'volume'
  return 'resolution_time'
}

function formatRunDate(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function buildSettingsSummary({
  mode,
  patternLimit,
  sourceSelection,
  days,
  minTickets,
  impactMetric,
  gapFilter,
  confidenceFilter,
  actionFilter,
}: {
  mode: AnalysisMode
  patternLimit: number
  sourceSelection: string
  days: string
  minTickets: string
  impactMetric: ImpactMetric
  gapFilter: 'all' | GapType
  confidenceFilter: 'all' | Confidence
  actionFilter: 'all' | RecommendedActionType
}) {
  const sourceText = SOURCE_SELECTION_LABELS[sourceSelection] ?? 'selected sources'
  const focusParts: string[] = []
  if (gapFilter !== 'all') focusParts.push(GAP_FILTER_SUMMARIES[gapFilter])
  if (confidenceFilter !== 'all') focusParts.push(CONFIDENCE_FILTER_SUMMARIES[confidenceFilter])
  if (actionFilter !== 'all') focusParts.push(ACTION_FILTER_SUMMARIES[actionFilter])

  const focusText = focusParts.length
    ? ` It will focus on findings ${focusParts.join(', ')}.`
    : ' It will keep the result broad so you can see missing, weak, stale, and conflicting documentation issues together.'

  return {
    title: MODE_SUMMARIES[mode],
    body: `Expected result: look at the last ${days} days and ignore small patterns below ${minTickets} tickets. It will use ${sourceText} and ${IMPACT_SUMMARIES[impactMetric]}.${focusText} It will check the top ${patternLimit} ticket ${patternLimit === 1 ? 'pattern' : 'patterns'}.`,
  }
}

function StatusBadge({
  label,
  value,
  tone = 'neutral',
}: {
  label: string
  value: string
  tone?: 'neutral' | 'good' | 'warning' | 'danger'
}) {
  const toneClass = {
    neutral: 'border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-300',
    good: 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300',
    warning: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300',
    danger: 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300',
  }[tone]

  return (
    <span
      aria-label={`${label}: ${value}`}
      className={`inline-flex min-h-6 cursor-default select-none items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] leading-tight ${toneClass}`}
    >
      <span className="font-medium opacity-70">{label}:</span>
      <span>{value}</span>
    </span>
  )
}

function coverageTone(status: string): 'neutral' | 'good' | 'warning' | 'danger' {
  if (status === 'good') return 'good'
  if (status === 'none' || status === 'stale' || status === 'conflicting') return 'danger'
  if (status === 'weak' || status === 'partial') return 'warning'
  return 'neutral'
}

function confidenceTone(confidence: string): 'neutral' | 'good' | 'warning' | 'danger' {
  if (confidence === 'high') return 'good'
  if (confidence === 'needs_review') return 'warning'
  return 'neutral'
}

function analysisProgress(elapsedSeconds: number) {
  if (elapsedSeconds < 4) {
    return {
      pct: 12,
      title: 'Preparing analysis',
      detail: 'Reading your choices and picking the ticket patterns to compare.',
    }
  }
  if (elapsedSeconds < 12) {
    return {
      pct: 34,
      title: 'Checking ticket evidence',
      detail: 'Looking for repeated issues, escalations, and useful sample tickets.',
    }
  }
  if (elapsedSeconds < 24) {
    return {
      pct: 58,
      title: 'Comparing knowledge sources',
      detail: 'Checking whether the selected docs explain those ticket patterns.',
    }
  }
  if (elapsedSeconds < 40) {
    return {
      pct: 76,
      title: 'Building gap findings',
      detail: 'Sorting the likely documentation gaps by usefulness.',
    }
  }
  return {
    pct: Math.min(92, 82 + Math.floor((elapsedSeconds - 40) / 6)),
    title: 'Finishing recommendations',
    detail: 'A wider search can take close to a minute. Please keep this page open.',
  }
}

function FindingCard({
  finding,
  active,
  onClick,
}: {
  finding: KnowledgeGapFinding
  active: boolean
  onClick: () => void
}) {
  const coverage = finding.source_coverage
    .map((c) => `${SOURCE_LABELS[c.source]}: ${COVERAGE_LABELS[c.status] ?? c.status}`)
    .join(' | ')
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full rounded-md border p-3 text-left transition-colors ${
        active ? 'border-primary bg-primary/5' : 'hover:bg-muted/40'
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="line-clamp-2 text-sm font-medium">{finding.title}</div>
          <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{finding.summary}</div>
        </div>
        <StatusBadge label="Gap" value={GAP_LABELS[finding.gap_type] ?? finding.gap_type} />
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <StatusBadge label="Impact" value={finding.impact_summary} />
        <StatusBadge
          label="Confidence"
          value={CONFIDENCE_LABELS[finding.confidence] ?? finding.confidence}
          tone={confidenceTone(finding.confidence)}
        />
      </div>
      <div className="mt-2 line-clamp-1 text-[11px] text-muted-foreground">Coverage by source: {coverage}</div>
    </button>
  )
}

function FindingDetail({
  finding,
  draft,
  draftLoading,
  draftError,
  activeDraftType,
  audience,
  onAudienceChange,
  onDraft,
}: {
  finding: KnowledgeGapFinding | null
  draft: DraftResult | null
  draftLoading: boolean
  draftError: string | null
  activeDraftType: DraftType | null
  audience: Audience
  onAudienceChange: (value: Audience) => void
  onDraft: (draftType: DraftType, targetSource?: KnowledgeSourceKind | null) => void
}) {
  if (!finding) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
        Run analysis and select a finding to review evidence, affected sources, and recommended actions.
      </div>
    )
  }

  const cluster = finding.ticket_cluster

  return (
    <div className="h-full overflow-y-auto">
      <div className="space-y-4 p-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge label="Gap" value={GAP_LABELS[finding.gap_type] ?? finding.gap_type} />
            <StatusBadge
              label="Confidence"
              value={CONFIDENCE_LABELS[finding.confidence] ?? finding.confidence}
              tone={confidenceTone(finding.confidence)}
            />
          </div>
          <h2 className="mt-3 text-lg font-semibold leading-tight">{finding.title}</h2>
          <p className="mt-2 text-sm text-muted-foreground">{finding.summary}</p>
        </div>

        <section className="rounded-md border">
          <div className="border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Ticket Evidence
          </div>
          <div className="space-y-3 p-3 text-sm">
            {cluster ? (
              <>
                <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                  <Metric label="Tickets" value={String(cluster.ticket_count)} />
                  <Metric label="Avg resolution" value={cluster.avg_resolution_hours != null ? `${cluster.avg_resolution_hours}h` : '-'} />
                  <Metric label="Avg open" value={cluster.avg_open_days != null ? `${cluster.avg_open_days}d` : '-'} />
                  <Metric label="Escalation" value={cluster.escalation_rate_pct != null ? `${cluster.escalation_rate_pct}%` : '-'} />
                </div>
                {cluster.common_symptoms.length > 0 && (
                  <div>
                    <div className="mb-1 text-xs font-medium text-muted-foreground">Common signals</div>
                    <ul className="space-y-1">
                      {cluster.common_symptoms.map((s, i) => (
                        <li key={`${s}-${i}`} className="text-sm">{s}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            ) : (
              <div className="text-muted-foreground">Ticket evidence is not available for this finding.</div>
            )}
          </div>
        </section>

        <section className="rounded-md border">
          <div className="border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Affected Sources
          </div>
          <div className="divide-y">
            {finding.source_coverage.map((coverage) => (
              <div key={coverage.source} className="space-y-2 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium">{SOURCE_LABELS[coverage.source]}</div>
                  <StatusBadge
                    label="Coverage"
                    value={COVERAGE_LABELS[coverage.status] ?? coverage.status}
                    tone={coverageTone(coverage.status)}
                  />
                </div>
                <p className="text-sm text-muted-foreground">{coverage.summary}</p>
                {coverage.source_specific_issue && (
                  <p className="text-sm">{coverage.source_specific_issue}</p>
                )}
                {coverage.documents.length > 0 && (
                  <div className="space-y-1">
                    {coverage.documents.map((doc) => (
                      <a
                        key={`${doc.source}-${doc.url}`}
                        href={doc.url}
                        target="_blank"
                        rel="noreferrer"
                        className="block rounded-md border px-2 py-1.5 text-sm hover:bg-muted/40"
                      >
                        <span className="font-medium">{doc.title}</span>
                        {doc.snippet && <span className="block line-clamp-1 text-xs text-muted-foreground">{doc.snippet}</span>}
                      </a>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-md border">
          <div className="border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Mismatch
          </div>
          <div className="space-y-3 p-3">
            {finding.mismatches.map((mismatch, i) => (
              <div key={i} className="space-y-1 text-sm">
                {mismatch.docs_claim && <p><span className="font-medium">Docs:</span> {mismatch.docs_claim}</p>}
                {mismatch.ticket_reality && <p><span className="font-medium">Tickets:</span> {mismatch.ticket_reality}</p>}
                <p><span className="font-medium">Conclusion:</span> {mismatch.conclusion}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-md border">
          <div className="border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Recommended Actions
          </div>
          <div className="divide-y">
            {finding.recommended_actions.map((action, i) => (
              <div key={`${action.action_type}-${i}`} className="space-y-1 p-3 text-sm">
                <div className="font-medium">{action.title}</div>
                <div className="text-muted-foreground">
                  {action.source ? SOURCE_LABELS[action.source] : 'All sources'} | Recommended doc action: {ACTION_LABELS[action.action_type] ?? action.action_type}
                </div>
                <p>{action.rationale}</p>
                {action.expected_impact && <p className="text-muted-foreground">{action.expected_impact}</p>}
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-md border">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Drafting Assistant
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Audience</span>
              <Select value={audience} onValueChange={(v) => onAudienceChange(v as Audience)}>
                <SelectTrigger className="h-8 w-[150px] text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(AUDIENCE_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>{label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="grid gap-2 p-3 md:grid-cols-2">
            {finding.draft_options.map((option) => (
              <button
                key={`${option.draft_type}-${option.target_source ?? 'general'}`}
                type="button"
                onClick={() => onDraft(option.draft_type, option.target_source)}
                className="rounded-md border px-3 py-2 text-left transition-colors hover:bg-muted/40"
              >
                <div className="text-sm font-medium">{option.label}</div>
                <p className="mt-1 text-xs text-muted-foreground">{option.description}</p>
                {draftLoading && activeDraftType === option.draft_type && (
                  <div className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" />
                    Drafting...
                  </div>
                )}
              </button>
            ))}
          </div>
          {draftError && (
            <div className="mx-3 mb-3 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {draftError}
            </div>
          )}
          {draft && (
            <div className="border-t p-3">
              <div className="mb-3">
                <div className="text-sm font-semibold">{draft.title}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  Review required: {draft.review_required ? 'yes' : 'no'}
                  {draft.target_source ? ` | Target: ${SOURCE_LABELS[draft.target_source]}` : ''}
                </div>
              </div>
              <div className="space-y-3">
                {draft.sections.map((section) => (
                  <div key={`${section.heading}-${section.basis}`} className="rounded-md border px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="text-sm font-medium">{section.heading}</div>
                      <StatusBadge label="Basis" value={DRAFT_BASIS_LABELS[section.basis] ?? section.basis} />
                    </div>
                    <div className="mt-2 whitespace-pre-wrap text-sm text-muted-foreground">{section.body}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm font-semibold">{value}</div>
    </div>
  )
}

function AnalysisProgress({ elapsedSeconds }: { elapsedSeconds: number }) {
  const progress = analysisProgress(elapsedSeconds)
  return (
    <div className="space-y-3 rounded-md border bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-medium">
            <Loader2 className="size-4 animate-spin text-primary" />
            {progress.title}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{progress.detail}</p>
        </div>
        <div className="shrink-0 text-right text-xs text-muted-foreground">
          <div>{elapsedSeconds}s</div>
          <div>usually 20-45s</div>
        </div>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-all duration-700"
          style={{ width: `${progress.pct}%` }}
        />
      </div>
      <div className="text-xs text-muted-foreground">
        ESIP is checking tickets against the selected docs. Broad choices may take a little longer.
      </div>
    </div>
  )
}

export function KnowledgeGapPage() {
  const [days, setDays] = useState('90')
  const [minTickets, setMinTickets] = useState('25')
  const [mode, setMode] = useState<AnalysisMode>('tickets_vs_all')
  const [patternLimit, setPatternLimit] = useState('3')
  const [impactMetric, setImpactMetric] = useState<ImpactMetric>('highest_time_cost')
  const [sourceSelection, setSourceSelection] = useState('all')
  const [gapFilter, setGapFilter] = useState<'all' | GapType>('all')
  const [confidenceFilter, setConfidenceFilter] = useState<'all' | Confidence>('all')
  const [actionFilter, setActionFilter] = useState<'all' | RecommendedActionType>('all')
  const [audience, setAudience] = useState<Audience>('engineer')
  const [result, setResult] = useState<KnowledgeGapAnalysisResult | null>(null)
  const [runs, setRuns] = useState<KnowledgeGapRunSummary[]>([])
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingContext, setLoadingContext] = useState<'analysis' | 'run' | null>(null)
  const [analysisStartedAt, setAnalysisStartedAt] = useState<number | null>(null)
  const [analysisElapsed, setAnalysisElapsed] = useState(0)
  const [runsLoading, setRunsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<DraftResult | null>(null)
  const [draftLoading, setDraftLoading] = useState(false)
  const [draftError, setDraftError] = useState<string | null>(null)
  const [activeDraftType, setActiveDraftType] = useState<DraftType | null>(null)

  const [legacyMessages, setLegacyMessages] = useState<LegacyMessage[]>([])
  const [legacyInput, setLegacyInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [showLegacy, setShowLegacy] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const chatBottomRef = useRef<HTMLDivElement>(null)

  const selectedFinding = useMemo(() => {
    if (!result?.findings.length) return null
    return result.findings.find((f) => f.finding_id === selectedFindingId) ?? result.findings[0]
  }, [result, selectedFindingId])

  const normalizedPatternLimit = clampPatternLimit(patternLimit)
  const depth: AnalysisDepth = normalizedPatternLimit <= 3 ? 'quick' : 'standard'

  const settingsSummary = useMemo(() => buildSettingsSummary({
    mode,
    patternLimit: normalizedPatternLimit,
    sourceSelection,
    days,
    minTickets,
    impactMetric,
    gapFilter,
    confidenceFilter,
    actionFilter,
  }), [
    mode,
    normalizedPatternLimit,
    sourceSelection,
    days,
    minTickets,
    impactMetric,
    gapFilter,
    confidenceFilter,
    actionFilter,
  ])

  useEffect(() => {
    setDraft(null)
    setDraftError(null)
  }, [selectedFindingId])

  useEffect(() => {
    void loadRuns()
  }, [])

  useEffect(() => {
    if (!loading || analysisStartedAt == null) return
    setAnalysisElapsed(Math.max(0, Math.floor((Date.now() - analysisStartedAt) / 1000)))
    const id = window.setInterval(() => {
      setAnalysisElapsed(Math.max(0, Math.floor((Date.now() - analysisStartedAt) / 1000)))
    }, 1000)
    return () => window.clearInterval(id)
  }, [loading, analysisStartedAt])

  async function loadRuns() {
    setRunsLoading(true)
    try {
      const resp = await fetch('/api/docs-gap/v2/runs', {
        headers: await authHeaders(),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json() as { runs: KnowledgeGapRunSummary[] }
      setRuns(data.runs)
    } catch {
      // Saved runs are helpful but should not block the workbench.
    } finally {
      setRunsLoading(false)
    }
  }

  async function runAnalysis() {
    if (loading || streaming) return
    setLoading(true)
    setLoadingContext('analysis')
    setAnalysisStartedAt(Date.now())
    setAnalysisElapsed(0)
    setError(null)
    setDraft(null)
    setDraftError(null)
    setShowLegacy(false)
    abortRef.current?.abort()

    try {
      const resp = await fetch('/api/docs-gap/v2/analyze', {
        method: 'POST',
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          mode,
          depth,
          pattern_limit: normalizedPatternLimit,
          period_days: parseInt(days, 10),
          min_tickets: parseInt(minTickets, 10),
          impact_metric: impactMetric,
          sources: sourceSelectionToList(sourceSelection),
          gap_types: gapFilter === 'all' ? [] : [gapFilter],
          audiences: [audience],
          action_types: actionFilter === 'all' ? [] : [actionFilter],
          confidence: confidenceFilter === 'all' ? [] : [confidenceFilter],
          scope: {
            brands: [],
            store_ids: [],
            regions: [],
            systems_or_vendors: [],
            issue_categories: [],
          },
        }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json() as KnowledgeGapAnalysisResult
      setResult(data)
      setSelectedFindingId(data.findings[0]?.finding_id ?? null)
      void loadRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Analysis failed')
    } finally {
      setLoading(false)
      setLoadingContext(null)
      setAnalysisStartedAt(null)
    }
  }

  async function openRun(runId: string) {
    if (loading || streaming) return
    setLoading(true)
    setLoadingContext('run')
    setAnalysisStartedAt(null)
    setAnalysisElapsed(0)
    setError(null)
    setShowLegacy(false)
    setDraft(null)
    setDraftError(null)
    try {
      const resp = await fetch(`/api/docs-gap/v2/runs/${encodeURIComponent(runId)}`, {
        headers: await authHeaders(),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const record = await resp.json() as KnowledgeGapRunRecord
      setResult(record.result)
      setSelectedFindingId(record.result.findings[0]?.finding_id ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to open saved analysis')
    } finally {
      setLoading(false)
      setLoadingContext(null)
    }
  }

  async function runDraft(draftType: DraftType, targetSource?: KnowledgeSourceKind | null) {
    if (!selectedFinding || draftLoading) return
    setDraftLoading(true)
    setDraftError(null)
    setActiveDraftType(draftType)
    try {
      const resp = await fetch('/api/docs-gap/v2/draft', {
        method: 'POST',
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          finding: selectedFinding,
          draft_type: draftType,
          audience,
          target_source: targetSource ?? null,
          instructions: null,
        }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json() as DraftResult
      setDraft(data)
    } catch (err) {
      setDraftError(err instanceof Error ? err.message : 'Draft failed')
    } finally {
      setDraftLoading(false)
      setActiveDraftType(null)
    }
  }

  async function runLegacyReport() {
    if (loading || streaming) return
    setShowLegacy(true)
    setLegacyMessages([])
    const prompt = `Analyze the top ticket categories for the last ${days} days and identify documentation gaps. Provide specific recommendations.`
    await streamLegacy([{ role: 'user', content: prompt }])
  }

  async function sendLegacyFollowUp(e: React.FormEvent) {
    e.preventDefault()
    const text = legacyInput.trim()
    if (!text || streaming) return
    setLegacyInput('')
    const nextMessages: LegacyMessage[] = [...legacyMessages, { role: 'user', content: text }]
    setLegacyMessages(nextMessages)
    await streamLegacy(nextMessages)
  }

  async function streamLegacy(messages: LegacyMessage[]) {
    setStreaming(true)
    setError(null)
    abortRef.current?.abort()
    abortRef.current = new AbortController()

    try {
      const resp = await fetch('/api/docs-gap/stream', {
        method: 'POST',
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          messages,
          days: parseInt(days, 10),
          min_tickets: parseInt(minTickets, 10),
          sort_by: impactToLegacySort(impactMetric),
        }),
        signal: abortRef.current.signal,
      })
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let assistantText = ''
      setLegacyMessages((prev) => [...prev, { role: 'assistant', content: '' }])

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const raw = decoder.decode(value, { stream: true })
        for (const line of raw.split('\n')) {
          if (!line.startsWith('data: ')) continue
          try {
            const ev = JSON.parse(line.slice(6))
            if (ev.delta) {
              assistantText += ev.delta
              setLegacyMessages((prev) => {
                const updated = [...prev]
                updated[updated.length - 1] = { role: 'assistant', content: assistantText }
                return updated
              })
              chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
            }
            if (ev.error) throw new Error(ev.error)
          } catch {
            // Ignore malformed SSE lines.
          }
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') return
      setError(err instanceof Error ? err.message : 'Legacy report failed')
    } finally {
      setStreaming(false)
    }
  }

  function reset() {
    abortRef.current?.abort()
    setResult(null)
    setSelectedFindingId(null)
    setLegacyMessages([])
    setLegacyInput('')
    setShowLegacy(false)
    setError(null)
    setDraft(null)
    setDraftError(null)
    setActiveDraftType(null)
    setLoading(false)
    setLoadingContext(null)
    setAnalysisStartedAt(null)
    setAnalysisElapsed(0)
    setStreaming(false)
  }

  return (
    <ResizablePanelGroup orientation="horizontal" className="h-full min-h-0 overflow-hidden">
      <ResizablePanel defaultSize={38} minSize={28}>
        <div className="flex h-full min-h-0 flex-col overflow-y-auto">
          <div className="flex items-center gap-3 border-b px-4 py-3">
            <BookOpen className="size-5 text-muted-foreground" />
            <div className="min-w-0">
              <h1 className="text-sm font-semibold">Knowledge Gaps</h1>
              <p className="text-[13px] text-sky-600 dark:text-sky-400">
                Evidence-backed documentation workbench
              </p>
            </div>
          </div>

          <div className="space-y-3 border-b bg-muted/20 p-4">
            <div className="rounded-md border bg-background px-3 py-2">
              <div className="text-sm font-medium leading-snug">{settingsSummary.title}</div>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{settingsSummary.body}</p>
            </div>

            <SettingsGroup title="What should we analyze?">
              <Control
                label="Analysis goal"
                description="Choose what you want to compare first."
              >
                <Select value={mode} onValueChange={(v) => setMode(v as AnalysisMode)}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="tickets_vs_all">Ticket reality vs all docs</SelectItem>
                    <SelectItem value="tickets_vs_zendesk">Zendesk article improvements</SelectItem>
                    <SelectItem value="tickets_vs_drive">Drive document improvements</SelectItem>
                    <SelectItem value="source_vs_source">Conflicts between sources</SelectItem>
                    <SelectItem value="knowledge_only">Docs-only audit</SelectItem>
                  </SelectContent>
                </Select>
              </Control>

              <Control
                label="Knowledge sources to check"
                description="Choose which docs should be checked against ticket reality."
              >
                <Select value={sourceSelection} onValueChange={setSourceSelection}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All sources</SelectItem>
                    <SelectItem value="zendesk">Zendesk Articles</SelectItem>
                    <SelectItem value="drive">Google Drive</SelectItem>
                    <SelectItem value="support_docs">Drive Support Docs</SelectItem>
                    <SelectItem value="job_aids">Drive Job Aids</SelectItem>
                    <SelectItem value="support_repository">Support Repository</SelectItem>
                  </SelectContent>
                </Select>
              </Control>

              <Control
                label="Ticket period"
                description="Shorter periods show recent problems; longer periods show more history."
              >
                <Select value={days} onValueChange={setDays}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="30">Last 30 days</SelectItem>
                    <SelectItem value="60">Last 60 days</SelectItem>
                    <SelectItem value="90">Last 90 days</SelectItem>
                    <SelectItem value="180">Last 6 months</SelectItem>
                    <SelectItem value="365">Last 12 months</SelectItem>
                  </SelectContent>
                </Select>
              </Control>

              <Control
                label="Minimum similar tickets"
                description="Higher numbers hide one-off issues; lower numbers show smaller patterns."
              >
                <Select value={minTickets} onValueChange={setMinTickets}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="5">5+</SelectItem>
                    <SelectItem value="10">10+</SelectItem>
                    <SelectItem value="25">25+</SelectItem>
                    <SelectItem value="50">50+</SelectItem>
                  </SelectContent>
                </Select>
              </Control>
            </SettingsGroup>

            <SettingsGroup title="What should we look for?">
              <Control
                label="Prioritize by"
                description="Choose which ticket patterns should appear first."
              >
                <Select value={impactMetric} onValueChange={(v) => setImpactMetric(v as ImpactMetric)}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="highest_time_cost">Biggest time cost</SelectItem>
                    <SelectItem value="most_frequent">Most repeated issues</SelectItem>
                    <SelectItem value="slowest_to_resolve">Slowest to close</SelectItem>
                    <SelectItem value="most_escalated">Most escalated</SelectItem>
                    <SelectItem value="most_reopened">Most reopened</SelectItem>
                    <SelectItem value="fastest_growing">Fastest growing</SelectItem>
                  </SelectContent>
                </Select>
              </Control>

              <Control
                label="Documentation problem"
                description="Limit results to one kind of documentation problem."
              >
                <Select value={gapFilter} onValueChange={(v) => setGapFilter(v as 'all' | GapType)}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All gap types</SelectItem>
                    {Object.entries(GAP_LABELS).map(([value, label]) => (
                      <SelectItem key={value} value={value}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Control>

              <Control
                label="Evidence strictness"
                description="Higher confidence means fewer findings; all confidence shows more to review."
              >
                <Select value={confidenceFilter} onValueChange={(v) => setConfidenceFilter(v as 'all' | Confidence)}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All confidence</SelectItem>
                    {Object.entries(CONFIDENCE_LABELS).map(([value, label]) => (
                      <SelectItem key={value} value={value}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Control>

              <Control
                label="Recommended doc fix"
                description="Limit results to the kind of doc change you want to make."
              >
                <Select value={actionFilter} onValueChange={(v) => setActionFilter(v as 'all' | RecommendedActionType)}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">Any doc action</SelectItem>
                    {Object.entries(ACTION_LABELS).map(([value, label]) => (
                      <SelectItem key={value} value={value}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Control>
            </SettingsGroup>

            <SettingsGroup title="How deep should it go?">
              <Control
                label="Issue patterns to check"
              >
                <Input
                  type="number"
                  min={PATTERN_LIMIT_MIN}
                  max={PATTERN_LIMIT_MAX}
                  step={1}
                  value={patternLimit}
                  onChange={(event) => setPatternLimit(event.target.value)}
                  onBlur={() => setPatternLimit(String(normalizedPatternLimit))}
                  className="h-8 w-24 text-sm"
                />
              </Control>
              <div className="rounded-md border border-dashed bg-background px-3 py-2 text-xs leading-snug text-muted-foreground">
                Checks the top {normalizedPatternLimit} ticket {normalizedPatternLimit === 1 ? 'pattern' : 'patterns'}. {patternLimitSummary(normalizedPatternLimit)}
              </div>
            </SettingsGroup>

            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => void runAnalysis()} disabled={loading || streaming} className="h-8">
                {loading ? <Loader2 className="mr-1.5 size-4 animate-spin" /> : <FileSearch className="mr-1.5 size-4" />}
                Analyze
              </Button>
              <Button size="sm" variant="outline" onClick={() => void runLegacyReport()} disabled={loading || streaming} className="h-8">
                {streaming ? <Loader2 className="mr-1.5 size-4 animate-spin" /> : <Sparkles className="mr-1.5 size-4" />}
                Legacy report
              </Button>
              {(result || legacyMessages.length > 0 || error) && (
                <Button size="sm" variant="ghost" onClick={reset} className="h-8 text-muted-foreground">
                  <RotateCcw className="size-3.5" />
                </Button>
              )}
            </div>
            <details className="rounded-md border bg-background">
              <summary className="flex h-9 cursor-pointer list-none items-center justify-between gap-2 px-3 text-xs font-medium uppercase tracking-wide text-muted-foreground hover:bg-muted/40">
                <span>Recent analyses</span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={(event) => {
                    event.preventDefault()
                    void loadRuns()
                  }}
                  disabled={runsLoading}
                  className="h-7 px-2 text-xs text-muted-foreground"
                >
                  {runsLoading ? <Loader2 className="size-3 animate-spin" /> : 'Refresh'}
                </Button>
              </summary>
              <div className="space-y-2 border-t p-2">
                {runs.length > 0 ? (
                  <div className="max-h-40 space-y-1 overflow-y-auto pr-1">
                    {runs.slice(0, 6).map((run) => (
                      <button
                        key={run.run_id}
                        type="button"
                        onClick={() => void openRun(run.run_id)}
                        className={`w-full rounded-md border px-2 py-1.5 text-left text-xs transition-colors hover:bg-muted/40 ${
                          result?.run_id === run.run_id ? 'border-primary bg-primary/5' : ''
                        }`}
                      >
                        <div className="line-clamp-1 font-medium">{run.title}</div>
                        <div className="mt-0.5 text-[11px] text-muted-foreground">
                          {formatRunDate(run.created_at)} | {run.finding_count} findings | {run.period_days} days
                        </div>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
                    Saved analyses will appear here after Analyze.
                  </div>
                )}
              </div>
            </details>
            {error && <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</div>}
            {result?.run_id && (
              <div className="rounded-md border bg-background px-3 py-2 text-xs text-muted-foreground">
                Saved as {formatRunDate(result.created_at ?? '')}
              </div>
            )}
          </div>

          <div className="p-3">
            {!result && !loading ? (
              <div className="flex h-full min-h-56 flex-col items-center justify-center gap-3 text-center text-sm text-muted-foreground">
                <BookOpen className="size-10 text-muted-foreground/40" />
                <p className="max-w-sm">
                  Run analysis to find where tickets and existing docs do not line up.
                </p>
              </div>
            ) : loading ? (
              loadingContext === 'analysis' ? (
                <AnalysisProgress elapsedSeconds={analysisElapsed} />
              ) : (
                <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  Opening saved analysis...
                </div>
              )
            ) : result && result.findings.length === 0 ? (
              <div className="flex h-full min-h-56 flex-col items-center justify-center gap-2 rounded-md border border-dashed p-4 text-center">
                <FileSearch className="size-8 text-muted-foreground/40" />
                <div className="text-sm font-medium">No matching gaps found</div>
                <p className="max-w-sm text-xs text-muted-foreground">
                  The current filters are very narrow. Try allowing all confidence levels,
                  all actions, or all gap types to see broader results.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {result?.findings.map((finding) => (
                  <FindingCard
                    key={finding.finding_id}
                    finding={finding}
                    active={selectedFinding?.finding_id === finding.finding_id}
                    onClick={() => setSelectedFindingId(finding.finding_id)}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </ResizablePanel>

      <ResizableHandle withHandle />

      <ResizablePanel defaultSize={62} minSize={38}>
        <div className="flex h-full flex-col overflow-hidden">
          <div className="flex items-center justify-between border-b px-4 py-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {showLegacy ? 'Legacy Gap Report' : 'Finding Detail'}
            </span>
            {result?.notes?.length ? <span className="text-xs text-muted-foreground">{result.notes[0]}</span> : null}
          </div>

          {showLegacy ? (
            <LegacyPanel
              messages={legacyMessages}
              input={legacyInput}
              streaming={streaming}
              onInput={setLegacyInput}
              onSubmit={sendLegacyFollowUp}
              bottomRef={chatBottomRef}
            />
          ) : (
            <FindingDetail
              finding={selectedFinding}
              draft={draft}
              draftLoading={draftLoading}
              draftError={draftError}
              activeDraftType={activeDraftType}
              audience={audience}
              onAudienceChange={setAudience}
              onDraft={runDraft}
            />
          )}
        </div>
      </ResizablePanel>
    </ResizablePanelGroup>
  )
}

function SettingsGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3 rounded-md border bg-background/60 p-3">
      <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</div>
      <div className="grid grid-cols-2 gap-3">{children}</div>
    </section>
  )
}

function Control({
  label,
  description,
  children,
}: {
  label: string
  description?: string
  children: React.ReactNode
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
      {description && <span className="text-[11px] leading-snug text-muted-foreground/80">{description}</span>}
    </label>
  )
}

function LegacyPanel({
  messages,
  input,
  streaming,
  onInput,
  onSubmit,
  bottomRef,
}: {
  messages: LegacyMessage[]
  input: string
  streaming: boolean
  onInput: (value: string) => void
  onSubmit: (e: React.FormEvent) => void
  bottomRef: React.RefObject<HTMLDivElement | null>
}) {
  return (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="flex flex-col gap-4 p-4">
          {messages.map((msg, i) => (
            <div key={i} className={msg.role === 'user' ? 'flex justify-end' : ''}>
              {msg.role === 'user' ? (
                <div className="max-w-[80%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
                  {msg.content}
                </div>
              ) : (
                <div className="prose prose-sm max-w-none text-sm dark:prose-invert">
                  <ReactMarkdown>{msg.content.replace(/SOURCES_JSON:.*$/m, '').trim()}</ReactMarkdown>
                  {streaming && i === messages.length - 1 && (
                    <span className="ml-0.5 inline-block size-1.5 animate-pulse rounded-full bg-primary align-middle" />
                  )}
                </div>
              )}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </div>
      <form onSubmit={onSubmit} className="flex gap-2 border-t p-3">
        <Input
          value={input}
          onChange={(e) => onInput(e.target.value)}
          placeholder={messages.length ? 'Ask a follow-up question...' : 'Run legacy report first'}
          disabled={!messages.length || streaming}
          className="h-8 text-sm"
        />
        <Button type="submit" size="sm" disabled={!messages.length || streaming || !input.trim()} className="h-8 px-3">
          {streaming ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
        </Button>
      </form>
    </>
  )
}
