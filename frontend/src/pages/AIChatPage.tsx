/**
 * AI Chat Page — split panel layout.
 *
 * Left panel:  ticket search / ticket detail (on-demand summary with language, no auto-load)
 * Right panel: Universal Chat — session tabs keep chat context independent from the left ticket panel
 *
 * URL state: selectedTicketId persisted as ?ticket=id so navigation away and back restores state.
 * Expand toggle: hides left panel, chat fills the full width.
 */
import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  ArrowLeft,
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Hash,
  Languages,
  Loader2,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  Search,
  Store,
  X,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { trackImpactEvent } from '@/lib/impactTelemetry'
import { planLookup } from '@/lib/ticketStoreSearch'
import { useProfile } from '@/contexts/ProfileContext'
import { cn, slugToTitle } from '@/lib/utils'
import type {
  KBArticleSource,
  TicketComment,
  TicketDetail,
  TicketListResponse,
  SummaryResponse,
  BriefSummary,
  DetailedSummary,
  CustomerRiskResponse,
} from '@/types/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from '@/components/ui/resizable'
import { UniversalChat } from '@/components/UniversalChat'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// ---------------------------------------------------------------------------
// Helpers / constants
// ---------------------------------------------------------------------------

const LANGUAGES = [
  { code: 'en', label: 'EN', name: 'English' },
  { code: 'fr', label: 'FR', name: 'French' },
  { code: 'es', label: 'ES', name: 'Spanish' },
  { code: 'zh', label: 'ZH', name: 'Chinese' },
  { code: 'ru', label: 'RU', name: 'Russian' },
  { code: 'hi', label: 'HI', name: 'Hindi' },
] as const

type LangCode = (typeof LANGUAGES)[number]['code']

function statusVariant(s: string): 'default' | 'secondary' | 'outline' | 'destructive' {
  if (s === 'open') return 'destructive'
  if (s === 'pending') return 'default'
  if (s === 'solved' || s === 'closed') return 'secondary'
  return 'outline'
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-CA', {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString('en-CA', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/** Brief vs detailed: do not use `'current_status' in s` alone — Gemini sometimes echoes brief-like keys on detailed JSON. */
function isBrief(s: SummaryResponse['summary']): s is BriefSummary {
  const row = s as unknown as Record<string, unknown>
  if (typeof row.markdown_text === 'string' && row.markdown_text.trim().length > 0) return false
  return typeof row.overview === 'string' && typeof row.current_status === 'string'
}

// ---------------------------------------------------------------------------
// Auto KB suggestions — loads on ticket open, no user action required
// ---------------------------------------------------------------------------

function KBSuggestionsBlock({ ticket }: { ticket: TicketDetail }) {
  const [result, setResult] = useState<{ ticketId: number; articles: KBArticleSource[] } | null>(null)

  // Build a rich query from ticket fields
  const query = [ticket.subject, ticket.issue_category, ticket.symptoms]
    .filter(Boolean)
    .join(' ')

  useEffect(() => {
    let active = true
    apiFetch<{ articles: KBArticleSource[] }>('/api/ai/kb-suggestions', {
      method: 'POST',
      body: JSON.stringify({ query, page_size: 5 }),
    })
      .then(response => {
        if (active) setResult({ ticketId: ticket.id, articles: response.articles })
      })
      .catch(() => {
        if (active) setResult({ ticketId: ticket.id, articles: [] })
      })
    return () => { active = false }
  }, [query, ticket.id])

  const loading = result?.ticketId !== ticket.id
  const articles = loading ? [] : result.articles

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-xs py-1">
        <Loader2 className="size-3 animate-spin" />
        Searching KB…
      </div>
    )
  }

  if (articles.length === 0) return null

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
        Suggested KB Articles
      </p>
      <div className="space-y-1.5">
        {articles.map((a, i) => <KBCard key={i} source={a} />)}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// KB Article card
// ---------------------------------------------------------------------------

function KBCard({ source }: { source: KBArticleSource }) {
  return (
    <a
      href={source.url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={() => void trackImpactEvent({
        event_type: 'document_link_clicked',
        workflow: 'Source clicks',
        object_type: 'kb_article',
        object_id: source.url,
        object_label: source.title,
        target_url: source.url,
      })}
      className="block rounded-lg border bg-muted/40 px-3 py-2 hover:bg-muted transition-colors text-sm"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-medium line-clamp-1">{source.title}</span>
        <ExternalLink className="size-3.5 shrink-0 mt-0.5 text-muted-foreground" />
      </div>
      {source.snippet && (
        <p className="text-xs text-muted-foreground mt-0.5 line-clamp-2">
          {source.snippet.replace(/<[^>]+>/g, '')}
        </p>
      )}
    </a>
  )
}

// ---------------------------------------------------------------------------
// On-demand AI Summary with language selector
// ---------------------------------------------------------------------------

type SummaryMode = 'brief' | 'detailed'

function AISummaryOnDemand({ ticketId }: { ticketId: number }) {
  const [activeMode, setActiveMode] = useState<SummaryMode | null>(null)
  const [language, setLanguage] = useState<LangCode>('en')
  const [data, setData] = useState<SummaryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const cacheRef = useRef<Record<string, SummaryResponse>>({})

  useEffect(() => {
    cacheRef.current = {}
    setData(null)
    setActiveMode(null)
  }, [ticketId])

  const load = async (m: SummaryMode, lang: LangCode = language, refresh = false) => {
    const key = `${ticketId}_${m}_${lang}`
    if (!refresh && cacheRef.current[key]) {
      setData(cacheRef.current[key])
      setActiveMode(m)
      setLanguage(lang)
      return
    }
    setLoading(true)
    setError('')
    setActiveMode(m)
    try {
      const res = await apiFetch<SummaryResponse>('/api/ai/summary', {
        method: 'POST',
        body: JSON.stringify({ ticket_id: ticketId, mode: m, language: lang, refresh }),
      })
      void trackImpactEvent({
        event_type: refresh ? 'summary_generated' : 'summary_viewed',
        workflow: 'Summary',
        ticket_id: ticketId,
        object_type: 'ticket_summary',
        object_id: `${ticketId}:${m}:${lang}`,
        metadata: { mode: m, language: lang, refresh },
      })
      cacheRef.current[key] = res
      setData(res)
      setActiveMode(m)
      setLanguage(lang)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load summary')
    } finally {
      setLoading(false)
    }
  }

  const switchLang = (lang: LangCode) => {
    if (activeMode) void load(activeMode, lang)
    else setLanguage(lang)
  }

  const summary = data?.summary
  const currentLang = LANGUAGES.find(l => l.code === language)!

  return (
    <div className="rounded-lg border bg-card flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b shrink-0 gap-1 flex-wrap">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">AI Summary</span>
        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant={activeMode === 'brief' ? 'default' : 'outline'}
            className="h-6 px-2 text-xs"
            disabled={loading}
            onClick={() => void load('brief', language)}
          >
            Brief
          </Button>
          <Button
            size="sm"
            variant={activeMode === 'detailed' ? 'default' : 'outline'}
            className="h-6 px-2 text-xs"
            disabled={loading}
            onClick={() => void load('detailed', language)}
          >
            Full
          </Button>

          <Separator orientation="vertical" className="h-4 mx-0.5" />

          {/* Language picker — right of Brief/Full */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="h-6 px-2 gap-1 text-xs">
                <Languages className="size-3" />
                {currentLang.label}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-36">
              {LANGUAGES.map(l => (
                <DropdownMenuItem key={l.code} onClick={() => switchLang(l.code)} className="gap-2">
                  <span className="font-mono text-xs w-6 shrink-0">{l.label}</span>
                  <span className="flex-1">{l.name}</span>
                  {language === l.code && <Check className="size-3 text-primary" />}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          {data && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-1.5"
              disabled={loading}
              onClick={() => void load(activeMode ?? 'brief', language, true)}
              title="Regenerate"
            >
              <RotateCcw className="size-3" />
            </Button>
          )}
        </div>
      </div>

      {/* Body — scrollable, vertically resizable */}
      <div
        className="overflow-y-auto p-3 text-sm space-y-3"
        style={{ minHeight: '6rem', resize: 'vertical' }}
      >
        {!activeMode && !loading && (
          <p className="text-xs text-muted-foreground italic">
            Click Brief or Full to generate an AI summary.
          </p>
        )}

        {loading && (
          <div className="flex items-center gap-2 text-muted-foreground py-4 justify-center">
            <Loader2 className="size-4 animate-spin" />
            Generating summary…
          </div>
        )}
        {error && !loading && <p className="text-xs text-destructive">{error}</p>}

        {summary && !loading && data && !(error && activeMode && data.mode !== activeMode) && (() => {
          const dAll = summary as DetailedSummary & BriefSummary
          const row = summary as unknown as Record<string, unknown>
          const kbSuggestions = dAll.kb_suggestions ?? []

          if (typeof dAll.markdown_text === 'string' && dAll.markdown_text.trim().length > 0) {
            return (
              <div className="space-y-3">
                <div className="prose prose-sm dark:prose-invert max-w-none text-sm
                  [&_h2]:text-sm [&_h2]:font-semibold [&_h2]:mt-4 [&_h2]:mb-1
                  [&_h3]:text-xs [&_h3]:font-medium [&_h3]:mt-3 [&_h3]:mb-1
                  [&_table]:text-xs [&_td]:py-0.5 [&_th]:py-0.5
                  [&_ul]:my-1 [&_li]:my-0 [&_p]:my-1">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {dAll.markdown_text}
                  </ReactMarkdown>
                </div>
                {kbSuggestions.length > 0 && (
                  <div>
                    <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">KB Articles</p>
                    <div className="space-y-1.5">
                      {kbSuggestions.map((s, i) => <KBCard key={i} source={s} />)}
                    </div>
                  </div>
                )}
              </div>
            )
          }

          const d = summary as DetailedSummary
          const participants    = d.participants ?? []
          const problemSummary  =
            (typeof d.problem_summary === 'string' && d.problem_summary) ||
            (typeof row.overview === 'string' ? row.overview : '') ||
            ''
          const timeline        = d.timeline ?? []
          const rootCause       = d.root_cause ?? null
          const actionsTaken    = d.actions_taken ?? []
          const finalStatus     = d.final_status ?? ''
          const additionalIssues = d.additional_issues ?? []
          const openQuestions   = d.open_questions ?? []
          const nextStepsArr    = d.next_steps ?? []

          const legacyBlock = (
            <div className="space-y-3">
              {participants.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Participants</p>
                  <ul className="space-y-0.5">{participants.map((p, i) => <li key={i} className="text-sm">{p}</li>)}</ul>
                </div>
              )}
              {problemSummary && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Problem Summary</p>
                  <p className="text-sm">{problemSummary}</p>
                </div>
              )}
              {timeline.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Timeline</p>
                  <ul className="space-y-1">
                    {timeline.map((e, i) => (
                      <li key={i} className="flex gap-1.5 text-sm">
                        <span className="text-muted-foreground shrink-0">·</span>{e}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {rootCause && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Root Cause</p>
                  <p className="text-sm">{rootCause}</p>
                </div>
              )}
              {actionsTaken.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Actions Taken</p>
                  <ul className="space-y-1">
                    {actionsTaken.map((a, i) => (
                      <li key={i} className="flex gap-1.5 text-sm">
                        <span className="text-muted-foreground shrink-0">•</span>{a}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {finalStatus && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Final Status</p>
                  <p className="text-sm">{finalStatus}</p>
                </div>
              )}
              {additionalIssues.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Additional Issues</p>
                  <ul className="space-y-1">
                    {additionalIssues.map((issue, i) => (
                      <li key={i} className="flex gap-1.5 text-sm">
                        <span className="text-muted-foreground shrink-0">·</span>{issue}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {openQuestions.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Open Questions</p>
                  <ul className="space-y-1">
                    {openQuestions.map((q, i) => (
                      <li key={i} className="flex gap-1.5 text-sm">
                        <span className="text-muted-foreground shrink-0">?</span>{q}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {nextStepsArr.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Next Steps</p>
                  <ul className="space-y-1">
                    {nextStepsArr.map((step, i) => (
                      <li key={i} className="flex gap-1.5 text-sm">
                        <span className="text-muted-foreground shrink-0">{i + 1}.</span>{step}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {kbSuggestions.length > 0 && (
                <div>
                  <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">KB Articles</p>
                  <div className="space-y-1.5">
                    {kbSuggestions.map((s, i) => <KBCard key={i} source={s} />)}
                  </div>
                </div>
              )}
            </div>
          )

          if (data.mode === 'detailed') return legacyBlock

          if (data.mode === 'brief' || isBrief(summary)) {
            const b = summary as BriefSummary
            const actions = b.key_actions_taken ?? []
            return (
              <div className="space-y-3">
                {b.overview && (
                  <div>
                    <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Overview</p>
                    <div className="prose prose-sm dark:prose-invert max-w-none text-sm [&_p]:my-1.5 [&_p:first-child]:mt-0">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{b.overview}</ReactMarkdown>
                    </div>
                  </div>
                )}
                {b.current_status && (
                  <div>
                    <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Status</p>
                    <p className="text-sm">{b.current_status}</p>
                  </div>
                )}
                {actions.length > 0 && (
                  <div>
                    <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">Key Actions</p>
                    <ul className="space-y-1">
                      {actions.map((a, i) => (
                        <li key={i} className="flex gap-1.5 text-sm items-baseline">
                          <span className="text-muted-foreground shrink-0">•</span>
                          <span className="[&_p]:inline [&_p]:m-0">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{a}</ReactMarkdown>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {(b.kb_suggestions ?? []).length > 0 && (
                  <div>
                    <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide mb-1">KB Articles</p>
                    <div className="space-y-1.5">
                      {(b.kb_suggestions ?? []).map((s, i) => <KBCard key={i} source={s} />)}
                    </div>
                  </div>
                )}
              </div>
            )
          }

          return legacyBlock
        })()}

        {data && !loading && !(error && activeMode && data.mode !== activeMode) && (
          <p className="text-xs text-muted-foreground border-t pt-2 mt-1">
            {data.from_cache ? 'Cached · ' : ''}Generated {formatDateTime(data.generated_at)}
          </p>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Expandable comment card
// ---------------------------------------------------------------------------

function CommentCard({ comment }: { comment: TicketComment }) {
  const [expanded, setExpanded] = useState(false)
  const isLong = comment.body.length > 300

  return (
    <div className="rounded-lg border bg-card overflow-hidden">
      <button
        type="button"
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-muted/40 transition-colors"
        onClick={() => setExpanded(e => !e)}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-medium text-foreground truncate">{comment.author_name}</span>
          <span className="text-xs text-muted-foreground shrink-0">{formatDate(comment.created_at)}</span>
        </div>
        {expanded
          ? <ChevronUp className="size-3.5 text-muted-foreground shrink-0" />
          : <ChevronDown className="size-3.5 text-muted-foreground shrink-0" />}
      </button>

      {expanded ? (
        <div
          className="px-3 pb-3 overflow-y-auto text-xs text-foreground whitespace-pre-wrap"
          style={{ minHeight: '3rem', resize: 'vertical' }}
        >
          {comment.body}
        </div>
      ) : (
        <div className="px-3 pb-2 text-xs text-muted-foreground line-clamp-2">
          {isLong ? comment.body.slice(0, 300) + '…' : comment.body}
        </div>
      )}
    </div>
  )
}

function customerRiskClasses(severity: CustomerRiskResponse['severity']) {
  if (severity === 'red') {
    return {
      shell: 'border-red-300 bg-red-50 text-red-950 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-100',
      badge: 'bg-red-600 text-white',
    }
  }
  if (severity === 'orange') {
    return {
      shell: 'border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-100',
      badge: 'bg-amber-600 text-white',
    }
  }
  if (severity === 'yellow') {
    return {
      shell: 'border-yellow-300 bg-yellow-50 text-yellow-950 dark:border-yellow-900/70 dark:bg-yellow-950/30 dark:text-yellow-100',
      badge: 'bg-yellow-500 text-black',
    }
  }
  return {
    shell: 'border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-900/70 dark:bg-emerald-950/30 dark:text-emerald-100',
    badge: 'bg-emerald-600 text-white',
  }
}

function CustomerRiskPanel({ ticketId }: { ticketId: number }) {
  const [result, setResult] = useState<{
    ticketId: number
    risk: CustomerRiskResponse | null
    loading: boolean
    error: string
  } | null>(null)

  useEffect(() => {
    let active = true
    apiFetch<CustomerRiskResponse>(`/api/tickets/${ticketId}/customer-risk`)
      .then(data => {
        if (!active) return
        setResult({ ticketId, risk: data, loading: false, error: '' })
        void trackImpactEvent({
          event_type: 'customer_risk_viewed',
          workflow: 'Customer Risk',
          ticket_id: ticketId,
          object_type: 'customer_risk',
          object_id: String(ticketId),
          metadata: { refresh: false },
        })
      })
      .catch(err => {
        if (active) {
          setResult({
            ticketId,
            risk: null,
            loading: false,
            error: err instanceof Error ? err.message : 'Customer risk unavailable',
          })
        }
      })
    return () => { active = false }
  }, [ticketId])

  const refresh = useCallback(() => {
    setResult(previous => ({
      ticketId,
      risk: previous?.ticketId === ticketId ? previous.risk : null,
      loading: true,
      error: '',
    }))
    apiFetch<CustomerRiskResponse>(`/api/tickets/${ticketId}/customer-risk?refresh=true`)
      .then(data => {
        setResult({ ticketId, risk: data, loading: false, error: '' })
        void trackImpactEvent({
          event_type: 'customer_risk_refreshed',
          workflow: 'Customer Risk',
          ticket_id: ticketId,
          object_type: 'customer_risk',
          object_id: String(ticketId),
          metadata: { refresh: true },
        })
      })
      .catch(err => {
        setResult(previous => ({
          ticketId,
          risk: previous?.ticketId === ticketId ? previous.risk : null,
          loading: false,
          error: err instanceof Error ? err.message : 'Customer risk unavailable',
        }))
      })
  }, [ticketId])

  const current = result?.ticketId === ticketId ? result : null
  const risk = current?.risk ?? null
  const loading = current?.loading ?? true
  const error = current?.error ?? ''

  if (loading && !risk) {
    return (
      <div className="rounded-lg border bg-card p-3 text-xs text-muted-foreground flex items-center gap-2">
        <Loader2 className="size-3.5 animate-spin" />
        Assessing customer risk...
      </div>
    )
  }

  if (error && !risk) {
    return (
      <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
        Customer Risk unavailable.
      </div>
    )
  }

  if (!risk) return null

  const classes = customerRiskClasses(risk.severity)
  const Icon = risk.severity === 'green' ? Check : AlertTriangle

  return (
    <div className={`rounded-lg border p-3 space-y-2 ${classes.shell}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Icon className="size-4 shrink-0" />
          <div className="min-w-0">
            <div className="text-xs font-semibold uppercase tracking-wide">Customer Risk</div>
            <div className="text-sm font-semibold leading-tight">{risk.label}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`rounded px-2 py-0.5 text-xs font-semibold uppercase ${classes.badge}`}>
            {risk.severity}
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            disabled={loading}
            title="Refresh customer risk"
            onClick={refresh}
          >
            {loading ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
          </Button>
        </div>
      </div>
      <p className="text-xs leading-relaxed">{risk.summary}</p>
      {risk.reasons.length > 0 && (
        <ul className="space-y-1 text-xs">
          {risk.reasons.slice(0, 3).map(reason => (
            <li key={reason} className="flex gap-1.5">
              <span className="mt-1 size-1 rounded-full bg-current opacity-70" />
              <span>{reason}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="rounded border border-current/20 bg-white/50 p-2 text-xs leading-relaxed dark:bg-black/10">
        <span className="font-semibold">Next: </span>{risk.next_action}
      </div>
      {error && <div className="text-[11px] opacity-70">Refresh failed; showing latest assessment.</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Compact ticket detail panel (shown in left panel)
// ---------------------------------------------------------------------------

function TicketDetailPanel({
  ticketId,
  onBack,
  onOpenStore,
}: {
  ticketId: number
  onBack: () => void
  onOpenStore?: (storeId: string) => void
}) {
  const { addTab } = useProfile()
  const [result, setResult] = useState<{
    ticketId: number
    ticket: TicketDetail | null
    error: string
  } | null>(null)

  useEffect(() => {
    let active = true
    apiFetch<TicketDetail>(`/api/tickets/${ticketId}`)
      .then(ticket => {
        if (active) setResult({ ticketId, ticket, error: '' })
      })
      .catch(err => {
        if (active) {
          setResult({ ticketId, ticket: null, error: err instanceof Error ? err.message : 'Failed' })
        }
      })
    return () => { active = false }
  }, [ticketId])

  const current = result?.ticketId === ticketId ? result : null
  const ticket = current?.ticket ?? null
  const loading = current === null
  const error = current?.error ?? ''

  useEffect(() => {
    if (ticket) {
      addTab({ id: ticket.id, subject: ticket.subject, status: ticket.status, priority: ticket.priority })
      void trackImpactEvent({
        event_type: 'ticket_page_opened',
        workflow: 'Ticket page',
        ticket_id: ticket.id,
        object_type: 'ticket',
        object_id: String(ticket.id),
        object_label: ticket.subject,
      })
    }
  }, [addTab, ticket])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-32 text-muted-foreground gap-2">
        <Loader2 className="size-4 animate-spin" />
        <span className="text-sm">Loading ticket…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 space-y-2">
        <p className="text-sm text-destructive">{error}</p>
        <Button variant="outline" size="sm" onClick={onBack}>
          <ArrowLeft className="size-3.5 mr-1.5" />Back to search
        </Button>
      </div>
    )
  }

  if (!ticket) return null

  const publicComments = ticket.comments.filter(c => !c.is_internal)

  return (
    <ScrollArea className="h-full">
      <div className="p-4 space-y-3">
        {/* Back + Zendesk link */}
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" className="size-7 shrink-0" onClick={onBack}>
            <ArrowLeft className="size-4" />
          </Button>
          <span className="font-mono text-muted-foreground text-sm">#{ticket.id}</span>
          <a href={`https://support.example.com/agent/tickets/${ticket.id}`} target="_blank" rel="noopener noreferrer">
            <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs text-muted-foreground">
              <ExternalLink className="size-3.5" />Zendesk
            </Button>
          </a>
        </div>

        {/* Subject + metadata */}
        <div className="rounded-lg border bg-card p-3 space-y-2">
          <h2 className="text-sm font-semibold leading-snug">{ticket.subject}</h2>
          <div className="flex flex-wrap gap-1.5 items-center">
            <Badge variant={statusVariant(ticket.status)} className="capitalize text-xs">{ticket.status}</Badge>
            {ticket.is_escalated && (
              <Badge variant="destructive" className="text-xs">
                <AlertTriangle className="size-2.5 mr-1" />Escalated
              </Badge>
            )}
          </div>
          <div className="text-xs text-muted-foreground space-y-1">
            {ticket.store && (
              <p>
                Store{' '}
                {onOpenStore ? (
                  <button
                    type="button"
                    className="font-medium text-foreground underline-offset-2 hover:underline"
                    onClick={() => onOpenStore(ticket.store!.store_id)}
                  >
                    #{ticket.store.store_id}
                  </button>
                ) : (
                  <span className="font-medium text-foreground">#{ticket.store.store_id}</span>
                )}
                {' · '}{ticket.store.org_name}
              </p>
            )}
            {ticket.issue_category && (
              <p>Category: <span className="text-foreground">{slugToTitle(ticket.issue_category)}</span></p>
            )}
            {ticket.symptoms && (
              <p>Symptoms: <span className="text-foreground">{ticket.symptoms}</span></p>
            )}
            {ticket.resolution && (
              <p>Resolution: <span className="text-foreground">{ticket.resolution}</span></p>
            )}
            <p className="text-muted-foreground/70">
              Created {formatDate(ticket.created_at)} · Updated {formatDate(ticket.updated_at)}
            </p>
          </div>
        </div>

        <CustomerRiskPanel ticketId={ticketId} />

        {/* AI Summary — on demand, full rendering, language selector */}
        <AISummaryOnDemand ticketId={ticketId} />

        {/* KB Suggestions — auto-loads on ticket open */}
        <KBSuggestionsBlock ticket={ticket} />

        {/* All comments — individually collapsible */}
        {publicComments.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              Comments ({publicComments.length})
            </p>
            {publicComments.map(c => (
              <CommentCard key={c.id} comment={c} />
            ))}
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

// ---------------------------------------------------------------------------
// Search panel
// ---------------------------------------------------------------------------

function buildSearchHelpDraft(q: string) {
  return `I typed "${q}" in the left search panel but found no matching ticket or store. Help me interpret what I need (ticket #, store #, or a broader search).`
}

function getUniversalStorageKey(ticketId: number | null): string {
  return ticketId ? `universal_chat_ticket_${ticketId}` : 'universal_chat_global'
}

type ChatSession = {
  id: string
  title: string
  contextType: 'global' | 'ticket'
  ticketId: number | null
  storageKey: string
  lastActiveAt: number
}

const CHAT_SESSIONS_STORAGE_KEY = 'universal_chat_sessions_v1'
const ACTIVE_CHAT_SESSION_STORAGE_KEY = 'universal_chat_active_session_v1'
const GLOBAL_CHAT_SESSION_ID = 'global'

function ticketChatSessionId(ticketId: number): string {
  return `ticket:${ticketId}`
}

function makeGlobalChatSession(): ChatSession {
  return {
    id: GLOBAL_CHAT_SESSION_ID,
    title: 'General',
    contextType: 'global',
    ticketId: null,
    storageKey: getUniversalStorageKey(null),
    lastActiveAt: Date.now(),
  }
}

function makeTicketChatSession(ticketId: number): ChatSession {
  return {
    id: ticketChatSessionId(ticketId),
    title: `#${ticketId}`,
    contextType: 'ticket',
    ticketId,
    storageKey: getUniversalStorageKey(ticketId),
    lastActiveAt: Date.now(),
  }
}

function hasStoredChatHistory(storageKey: string): boolean {
  try {
    const raw = localStorage.getItem(storageKey)
    if (!raw) return false
    const parsed = JSON.parse(raw) as { messages?: unknown[] }
    return Array.isArray(parsed.messages) && parsed.messages.length > 0
  } catch {
    return false
  }
}

function normalizeChatSessions(raw: unknown, initialTicketId: number | null): ChatSession[] {
  const now = Date.now()
  const byId = new Map<string, ChatSession>()
  byId.set(GLOBAL_CHAT_SESSION_ID, makeGlobalChatSession())

  if (Array.isArray(raw)) {
    for (const row of raw) {
      if (!row || typeof row !== 'object') continue
      const candidate = row as Partial<ChatSession>
      if (candidate.contextType === 'global') {
        byId.set(GLOBAL_CHAT_SESSION_ID, {
          ...makeGlobalChatSession(),
          lastActiveAt: typeof candidate.lastActiveAt === 'number' ? candidate.lastActiveAt : now,
        })
      } else if (candidate.contextType === 'ticket' && typeof candidate.ticketId === 'number') {
        const ticketSession = makeTicketChatSession(candidate.ticketId)
        byId.set(ticketSession.id, {
          ...ticketSession,
          lastActiveAt: typeof candidate.lastActiveAt === 'number' ? candidate.lastActiveAt : now,
        })
      }
    }
  }

  if (initialTicketId) {
    const ticketSession = makeTicketChatSession(initialTicketId)
    byId.set(ticketSession.id, byId.get(ticketSession.id) ?? ticketSession)
  }

  const ticketSessions = [...byId.values()].filter(s => s.contextType === 'ticket')

  return [byId.get(GLOBAL_CHAT_SESSION_ID) ?? makeGlobalChatSession(), ...ticketSessions]
}

function loadChatSessionState(initialTicketId: number | null): {
  sessions: ChatSession[]
  activeSessionId: string
} {
  let raw: unknown = null
  try {
    raw = JSON.parse(localStorage.getItem(CHAT_SESSIONS_STORAGE_KEY) || 'null')
  } catch {
    raw = null
  }
  const sessions = normalizeChatSessions(raw, initialTicketId)
  const savedActiveId = (() => {
    try {
      return localStorage.getItem(ACTIVE_CHAT_SESSION_STORAGE_KEY)
    } catch {
      return null
    }
  })()
  const savedActive = sessions.find(s => s.id === savedActiveId)
  const initialTicketSessionId = initialTicketId ? ticketChatSessionId(initialTicketId) : null

  if (savedActive) {
    if (
      initialTicketSessionId &&
      savedActive.id === GLOBAL_CHAT_SESSION_ID &&
      !hasStoredChatHistory(savedActive.storageKey)
    ) {
      return { sessions, activeSessionId: initialTicketSessionId }
    }
    return { sessions, activeSessionId: savedActive.id }
  }

  return {
    sessions,
    activeSessionId: initialTicketSessionId ?? GLOBAL_CHAT_SESSION_ID,
  }
}

function saveChatSessions(sessions: ChatSession[]) {
  try {
    localStorage.setItem(CHAT_SESSIONS_STORAGE_KEY, JSON.stringify(sessions))
  } catch {
    // Session state remains available in memory when storage is unavailable.
  }
}

function saveActiveChatSession(activeSessionId: string) {
  try {
    localStorage.setItem(ACTIVE_CHAT_SESSION_STORAGE_KEY, activeSessionId)
  } catch {
    // Session state remains available in memory when storage is unavailable.
  }
}

function ChatSessionTabs({
  sessions,
  activeSessionId,
  selectedTicketId,
  activeTicketId,
  onSelectSession,
  onCloseSession,
  onReorderSession,
  onStartTicketChat,
}: {
  sessions: ChatSession[]
  activeSessionId: string
  selectedTicketId: number | null
  activeTicketId: number | null
  onSelectSession: (sessionId: string) => void
  onCloseSession: (sessionId: string) => void
  onReorderSession: (fromSessionId: string, toSessionId: string) => void
  onStartTicketChat: (ticketId: number) => void
}) {
  const [draggingSessionId, setDraggingSessionId] = useState<string | null>(null)
  const [dragOverSessionId, setDragOverSessionId] = useState<string | null>(null)
  const showTicketChatAction = selectedTicketId != null && activeTicketId !== selectedTicketId

  return (
    <div className="flex h-11 shrink-0 items-center gap-2 border-b bg-muted/20 px-2">
      <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
        {sessions.map(session => {
          const active = session.id === activeSessionId
          const draggable = session.contextType === 'ticket'
          const dragOver = draggable && draggingSessionId !== session.id && dragOverSessionId === session.id
          return (
            <div
              key={session.id}
              draggable={draggable}
              onDragStart={(event) => {
                if (!draggable) return
                setDraggingSessionId(session.id)
                event.dataTransfer.effectAllowed = 'move'
                event.dataTransfer.setData('text/plain', session.id)
              }}
              onDragOver={(event) => {
                if (!draggable || !draggingSessionId || draggingSessionId === session.id) return
                event.preventDefault()
                event.dataTransfer.dropEffect = 'move'
                setDragOverSessionId(session.id)
              }}
              onDragLeave={() => {
                if (dragOverSessionId === session.id) setDragOverSessionId(null)
              }}
              onDrop={(event) => {
                if (!draggable) return
                event.preventDefault()
                const fromId = event.dataTransfer.getData('text/plain') || draggingSessionId
                if (fromId) onReorderSession(fromId, session.id)
                setDraggingSessionId(null)
                setDragOverSessionId(null)
              }}
              onDragEnd={() => {
                setDraggingSessionId(null)
                setDragOverSessionId(null)
              }}
              className={cn(
                'flex h-8 shrink-0 items-center rounded-md border text-xs transition-colors',
                active
                  ? 'border-border bg-background shadow-xs'
                  : 'border-transparent bg-transparent text-muted-foreground hover:bg-muted',
                draggable && 'cursor-grab active:cursor-grabbing',
                draggingSessionId === session.id && 'opacity-60',
                dragOver && 'border-primary bg-primary/10 text-foreground'
              )}
            >
              <button
                type="button"
                onClick={() => onSelectSession(session.id)}
                className="flex h-full items-center gap-1.5 px-2"
                title={session.contextType === 'ticket' ? `Ticket chat ${session.title}. Drag to reorder.` : 'General chat'}
              >
                <MessageSquare className="size-3.5" />
                <span className={cn(session.contextType === 'ticket' && 'font-mono')}>
                  {session.title}
                </span>
              </button>
              {session.contextType === 'ticket' && (
                <button
                  type="button"
                  onClick={() => onCloseSession(session.id)}
                  className="mr-1 flex size-5 items-center justify-center rounded text-muted-foreground hover:bg-muted-foreground/10 hover:text-foreground"
                  title={`Close ${session.title}`}
                  aria-label={`Close ${session.title}`}
                >
                  <X className="size-3" />
                </button>
              )}
            </div>
          )
        })}
      </div>
      {showTicketChatAction && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-8 shrink-0 gap-1.5 px-2 text-xs"
          onClick={() => onStartTicketChat(selectedTicketId)}
          title={`Open ticket chat #${selectedTicketId}`}
        >
          <MessageSquare className="size-3.5" />
          <span>Chat</span>
          <span className="font-mono">#{selectedTicketId}</span>
        </Button>
      )}
    </div>
  )
}

function SearchPanel({
  onSelectTicket,
  pendingStoreId,
  onConsumePendingStore,
  onRequestAiDraft,
}: {
  onSelectTicket: (id: number) => void
  pendingStoreId: string | null
  onConsumePendingStore: () => void
  onRequestAiDraft: (text: string) => void
}) {
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [hint, setHint] = useState('')
  const [error, setError] = useState('')
  const [ticketIds, setTicketIds] = useState<number[]>([])
  const [storeResults, setStoreResults] = useState<TicketListResponse | null>(null)
  const [currentStoreId, setCurrentStoreId] = useState<string | null>(null)
  const [aiHelpSeed, setAiHelpSeed] = useState<string | null>(null)
  const storeCacheRef = useRef<Map<string, TicketListResponse>>(new Map())
  const { profile, addSearchHistory } = useProfile()

  const resetResults = () => {
    setTicketIds([])
    setStoreResults(null)
    setCurrentStoreId(null)
    setHint('')
    setError('')
    setAiHelpSeed(null)
  }

  useEffect(() => {
    if (!pendingStoreId) return
    resetResults()
    setQuery(pendingStoreId)
    void (async () => {
      setLoading(true)
      try {
        addSearchHistory(pendingStoreId, 'store')
        const data = await apiFetch<TicketListResponse>(`/api/tickets/by-store/${pendingStoreId}`)
        storeCacheRef.current.set(pendingStoreId, data)
        setStoreResults(data)
        setCurrentStoreId(pendingStoreId)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Search failed')
        setAiHelpSeed(pendingStoreId)
      } finally {
        setLoading(false)
        onConsumePendingStore()
      }
    })()
  }, [pendingStoreId, onConsumePendingStore, addSearchHistory])

  const fetchStore = async (storeId: string, bustCache = false): Promise<boolean> => {
    if (bustCache) storeCacheRef.current.delete(storeId)
    const cached = storeCacheRef.current.get(storeId)
    if (cached) {
      setStoreResults(cached)
      setCurrentStoreId(storeId)
      return true
    }
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch<TicketListResponse>(`/api/tickets/by-store/${storeId}`)
      storeCacheRef.current.set(storeId, data)
      setStoreResults(data)
      setCurrentStoreId(storeId)
      return true
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed')
      return false
    } finally {
      setLoading(false)
    }
  }

  const handleSearch = async (e: FormEvent) => {
    e.preventDefault()
    const raw = query.trim()
    if (!raw) return

    resetResults()

    const plan = planLookup(raw)
    if (plan.kind === 'invalid') {
      setError(plan.message)
      setAiHelpSeed(raw)
      return
    }

    if (plan.kind === 'explicit_store') {
      addSearchHistory(plan.historyQuery, 'store')
      const ok = await fetchStore(plan.storeId)
      if (!ok) setAiHelpSeed(plan.historyQuery)
      return
    }

    if (plan.kind === 'ticket_ids') {
      if (plan.truncated) setHint('Only the first 4 ticket IDs will be shown.')
      addSearchHistory(plan.historyQuery, 'ticket')
      if (plan.ids.length === 1) {
        onSelectTicket(plan.ids[0])
        return
      }
      setTicketIds(plan.ids)
      return
    }

    if (plan.kind === 'store_from_text') {
      if (plan.hasMultiple) {
        setHint(`Multiple store numbers detected. Showing results for store ${plan.storeId} only.`)
      }
      addSearchHistory(plan.historyQuery, 'store')
      const ok = await fetchStore(plan.storeId)
      if (!ok) setAiHelpSeed(plan.historyQuery)
      return
    }

    setError('Unsupported numeric search format.')
    setAiHelpSeed(raw)
  }

  const historyItems = profile?.search_history ?? []
  const showHistoryChips =
    historyItems.length > 0 && !storeResults && ticketIds.length === 0

  return (
    <div className="h-full overflow-y-auto p-6 space-y-6">
      <div>
        <p className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">AI Chat</p>
        <h2 className="text-xl font-semibold">Ticket Context</h2>
        <p className="text-[13px] text-sky-600 dark:text-sky-400 mt-0.5">
          Find and open the right ticket context here. Search by ticket #, list/range, 4-digit store, store:2002 / s2002, or #1048 to force a ticket. Or start directly in Conversation.
        </p>
      </div>

      <form onSubmit={(e) => void handleSearch(e)} className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. 950014, 1048, #1048, store:2002"
          className="max-w-md"
          autoFocus
        />
        <Button type="submit" disabled={loading || !query.trim()}>
          {loading ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
          Search
        </Button>
      </form>

      {showHistoryChips && (
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">Recent searches</p>
          <div className="flex flex-wrap gap-1.5">
            {historyItems.map((item, i) => (
              <button
                key={i}
                type="button"
                onClick={() => {
                  setQuery(item.query)
                  resetResults()
                }}
                className="inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                {item.type === 'ticket' ? <Hash className="size-3" /> : <Store className="size-3" />}
                {item.query}
              </button>
            ))}
          </div>
        </div>
      )}

      {hint && <p className="text-sm text-muted-foreground">{hint}</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {aiHelpSeed && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              onRequestAiDraft(buildSearchHelpDraft(aiHelpSeed))
              setAiHelpSeed(null)
            }}
          >
            Ask AI…
          </Button>
          <span className="text-xs text-muted-foreground">Fills the chat input — send when ready.</span>
        </div>
      )}

      {ticketIds.length > 0 && (
        <div className="space-y-2">
          <p className="text-sm font-medium text-muted-foreground">
            Found {ticketIds.length} tickets — click to open in panel:
          </p>
          <div className="flex flex-wrap gap-2">
            {ticketIds.map((id) => (
              <Button key={id} variant="outline" size="sm" className="font-mono" onClick={() => onSelectTicket(id)}>
                #{id}
              </Button>
            ))}
          </div>
        </div>
      )}

      {storeResults && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <span className="font-medium">{storeResults.org_name}</span>
              {' '}
              <span className="text-muted-foreground text-sm">— {storeResults.total} tickets</span>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={loading}
              onClick={() => {
                if (currentStoreId) void fetchStore(currentStoreId, true)
              }}
            >
              {loading ? <Loader2 className="size-3.5 animate-spin mr-1.5" /> : <RefreshCw className="size-3.5 mr-1.5" />}
              Refresh
            </Button>
          </div>
          <ScrollArea className="h-[500px] rounded-md border">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 sticky top-0">
                <tr>
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground">#</th>
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground">Subject</th>
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground">Status</th>
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground">Updated</th>
                </tr>
              </thead>
              <tbody>
                {storeResults.tickets.map((t) => (
                  <tr
                    key={t.id}
                    className="border-t hover:bg-muted/40 cursor-pointer transition-colors"
                    onClick={() => onSelectTicket(t.id)}
                  >
                    <td className="px-4 py-2.5 font-mono text-muted-foreground">{t.id}</td>
                    <td className="px-4 py-2.5 max-w-xs">
                      <span className="line-clamp-1">{t.subject}</span>
                      {t.issue_category && (
                        <span className="text-xs text-muted-foreground block">{slugToTitle(t.issue_category)}</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge variant={statusVariant(t.status)} className="capitalize">
                        {t.status}
                      </Badge>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground whitespace-nowrap">
                      {formatDate(t.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollArea>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function AIChatPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [chatExpanded, setChatExpanded] = useState(false)
  const [pendingStoreSearch, setPendingStoreSearch] = useState<string | null>(null)
  const [chatDraftMessage, setChatDraftMessage] = useState<string | null>(null)

  const selectedTicketId = (() => {
    const t = searchParams.get('ticket')
    if (!t) return null
    const parsed = parseInt(t, 10)
    return Number.isFinite(parsed) ? parsed : null
  })()
  const [chatSessions, setChatSessions] = useState<ChatSession[]>(() => loadChatSessionState(selectedTicketId).sessions)
  const [activeChatSessionId, setActiveChatSessionId] = useState<string>(() => loadChatSessionState(selectedTicketId).activeSessionId)
  const activeChatSession = chatSessions.find(s => s.id === activeChatSessionId) ?? chatSessions[0] ?? makeGlobalChatSession()
  const activeChatTicketId = activeChatSession.contextType === 'ticket' ? activeChatSession.ticketId : null

  const ensureTicketChatSession = useCallback((ticketId: number, activate: boolean) => {
    const sessionId = ticketChatSessionId(ticketId)
    setChatSessions(prev => {
      const now = Date.now()
      const exists = prev.some(s => s.id === sessionId)
      return exists
        ? prev.map(s => s.id === sessionId ? { ...s, lastActiveAt: now } : s)
        : [...prev, makeTicketChatSession(ticketId)]
    })
    if (activate) setActiveChatSessionId(sessionId)
  }, [])

  useEffect(() => {
    saveChatSessions(chatSessions)
  }, [chatSessions])

  useEffect(() => {
    saveActiveChatSession(activeChatSessionId)
  }, [activeChatSessionId])

  useEffect(() => {
    if (!selectedTicketId) return
    // URL selection is an external navigation source that must be mirrored to session state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    ensureTicketChatSession(selectedTicketId, false)
  }, [selectedTicketId, ensureTicketChatSession])

  const consumePendingStore = useCallback(() => {
    setPendingStoreSearch(null)
  }, [])

  const applyChatDraft = useCallback((text: string) => {
    setChatDraftMessage(text)
  }, [])

  const clearChatDraft = useCallback(() => {
    setChatDraftMessage(null)
  }, [])

  // Persist current chat URL to sessionStorage so sidebar can restore it
  useEffect(() => {
    const search = searchParams.toString()
    sessionStorage.setItem('lastChatSearch', search)
  }, [searchParams])

  const handleSelectTicket = useCallback((id: number | null) => {
    // Left-panel navigation should not steal the active chat tab.
    if (id) ensureTicketChatSession(id, false)
    if (id) setSearchParams({ ticket: String(id) }, { replace: true })
    else setSearchParams({}, { replace: true })
  }, [ensureTicketChatSession, setSearchParams])

  const handleBackToSearch = useCallback(() => {
    setSearchParams({}, { replace: true })
  }, [setSearchParams])

  const handleOpenTicketFromChat = useCallback((id: number) => {
    ensureTicketChatSession(id, false)
    setSearchParams({ ticket: String(id) }, { replace: true })
  }, [ensureTicketChatSession, setSearchParams])

  const handleSelectChatSession = useCallback((sessionId: string) => {
    const session = chatSessions.find(s => s.id === sessionId)
    if (!session) return
    setActiveChatSessionId(sessionId)
    setChatSessions(prev => prev.map(s => s.id === sessionId ? { ...s, lastActiveAt: Date.now() } : s))
    if (session.contextType === 'ticket' && session.ticketId) {
      setSearchParams({ ticket: String(session.ticketId) }, { replace: true })
    }
  }, [chatSessions, setSearchParams])

  const handleCloseChatSession = useCallback((sessionId: string) => {
    if (sessionId === GLOBAL_CHAT_SESSION_ID) return
    setChatSessions(prev => prev.filter(s => s.id !== sessionId))
    if (activeChatSessionId === sessionId) {
      setActiveChatSessionId(GLOBAL_CHAT_SESSION_ID)
    }
  }, [activeChatSessionId])

  const handleReorderChatSession = useCallback((fromSessionId: string, toSessionId: string) => {
    if (fromSessionId === toSessionId) return
    if (fromSessionId === GLOBAL_CHAT_SESSION_ID || toSessionId === GLOBAL_CHAT_SESSION_ID) return
    setChatSessions(prev => {
      const fromIndex = prev.findIndex(s => s.id === fromSessionId)
      const toIndex = prev.findIndex(s => s.id === toSessionId)
      if (fromIndex < 0 || toIndex < 0) return prev
      const next = [...prev]
      const [moved] = next.splice(fromIndex, 1)
      next.splice(toIndex, 0, moved)
      return next
    })
  }, [])

  const handleStartTicketChat = useCallback((ticketId: number) => {
    ensureTicketChatSession(ticketId, true)
    setSearchParams({ ticket: String(ticketId) }, { replace: true })
  }, [ensureTicketChatSession, setSearchParams])

  return (
    <ResizablePanelGroup orientation="horizontal" className="h-full">
      {!chatExpanded && (
        <>
          <ResizablePanel defaultSize={50} minSize={25} className="min-h-0">
            {selectedTicketId === null ? (
              <SearchPanel
                onSelectTicket={handleSelectTicket}
                pendingStoreId={pendingStoreSearch}
                onConsumePendingStore={consumePendingStore}
                onRequestAiDraft={applyChatDraft}
              />
            ) : (
              <TicketDetailPanel
                ticketId={selectedTicketId}
                onBack={handleBackToSearch}
                onOpenStore={(storeId) => {
                  handleBackToSearch()
                  setPendingStoreSearch(storeId)
                }}
              />
            )}
          </ResizablePanel>
          <ResizableHandle withHandle />
        </>
      )}

      <ResizablePanel defaultSize={chatExpanded ? 100 : 50} minSize={25} className="flex min-h-0 flex-col">
        <ChatSessionTabs
          sessions={chatSessions}
          activeSessionId={activeChatSession.id}
          selectedTicketId={selectedTicketId}
          activeTicketId={activeChatTicketId}
          onSelectSession={handleSelectChatSession}
          onCloseSession={handleCloseChatSession}
          onReorderSession={handleReorderChatSession}
          onStartTicketChat={handleStartTicketChat}
        />
        <div className="min-h-0 flex-1">
          <UniversalChat
            key={activeChatSession.id}
            ticketId={activeChatTicketId}
            onTicketFound={handleOpenTicketFromChat}
            expanded={chatExpanded}
            onExpandToggle={() => setChatExpanded(e => !e)}
            storageKey={activeChatSession.storageKey}
            draftMessage={chatDraftMessage}
            onDraftMessageApplied={clearChatDraft}
          />
        </div>
      </ResizablePanel>
    </ResizablePanelGroup>
  )
}
