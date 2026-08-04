import {
  Fragment, useEffect, useState, useCallback, useMemo, useRef,
  type ReactNode, type CSSProperties, type MouseEvent as ReactMouseEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  LayoutDashboard, Loader2, ShieldCheck, CheckCircle2, Database,
  DollarSign, Bell, AlertTriangle, X, Plus, RefreshCw, GripHorizontal, Lock,
  ChevronUp, ChevronDown, MessageSquareText, Network, Activity, FileSearch, UsersRound, Info, Eye, EyeOff,
  Pencil, Save, Printer, TrendingUp, Clock3, Search, Trash2, Check, Download,
} from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  Legend,
} from 'recharts'
import { GridLayout, useContainerWidth } from 'react-grid-layout'
import type { LayoutItem, Layout } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'
import { useAuth } from '@/contexts/AuthContext'
import { apiFetch, authHeaders } from '@/lib/api'
import { Switch } from '@/components/ui/switch'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PageScroll } from '@/components/PageScroll'
import { REFERENCE_TENANT_DOMAIN } from '@/reference_data/fixtures'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'


// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type AdminPanelTab = 'costs' | 'system' | 'notifications' | 'users' | 'usage' | 'impact' | 'changes' | 'quality' | 'chat-logs'
type InsightMode = 'esip' | 'kpi' | 'kpi-email'

const ADMIN_TAB_PATHS: Record<AdminPanelTab, string> = {
  costs: '/admin/costs',
  system: '/admin/system',
  notifications: '/admin/notifications',
  users: '/admin/users',
  usage: '/admin/insights/esip-usage',
  impact: '/admin/impact',
  changes: '/admin/changes',
  quality: '/admin/quality',
  'chat-logs': '/admin/chat-logs',
}

const isDateInputValue = (value: string | null | undefined): value is string =>
  Boolean(value && /^\d{4}-\d{2}-\d{2}$/.test(value))

interface CostSnapshot {
  currency?: 'CAD'
  today: {
    // Prefer *_cad when present; *_usd remains for backward compatibility.
    date: string
    cost_usd: number
    cost_cad?: number
    calls: number
    by_type: Record<string, { cost_usd: number; cost_cad?: number; calls: number }>
    pct_of_daily_limit: number
  }
  history: Array<{ date: string; cost_usd: number; cost_cad?: number; calls: number }>
  budget: {
    daily_limit_usd: number
    daily_limit_cad?: number
    monthly_limit_usd: number
    monthly_limit_cad?: number
    soft_cap_pct: number
    hard_cap_pct: number
    divergence_threshold: number
  }
  alert: {
    type: string
    message: string
    at: string
    hard_cap_active: boolean
  } | null
  note: string
}

interface RangeData {
  from_dt: string
  to_dt: string
  granularity: 'hourly' | 'daily'
  // Prefer *_cad when present; *_usd remains for backward compatibility.
  total_cost_usd: number
  total_cost_cad?: number
  total_calls: number
  by_type: Record<string, { cost_usd: number; cost_cad?: number; calls: number; tokens_in: number; tokens_out: number }>
  buckets: Array<{ key: string; label: string; cost_usd: number; cost_cad?: number; calls: number }>
}

interface BillingData {
  from_dt: string
  to_dt: string
  // Prefer *_cad when present; *_usd remains for backward compatibility.
  total_cost_usd: number
  total_cost_cad?: number
  buckets: Array<{ key: string; label: string; cost_usd: number; cost_cad?: number }>
  latest_day: string | null
  lag_note: string
}

interface ReconciliationServiceRow {
  day: string
  service: string
  billed_cad: number
  net_billed_cad: number
  app_actual_cad: number
  app_estimated_cad: number
  untracked_cad: number
  coverage_pct: number | null
  app_actual_events: number
  call_types: string | null
  billing_rows: number
}

interface ReconciliationData {
  from_dt: string
  to_dt: string
  currency: 'CAD'
  totals: {
    billed_cad: number
    credits_cad: number
    net_billed_cad: number
    app_actual_cad: number
    app_estimated_cad: number
    untracked_cad: number
    coverage_pct: number | null
  }
  buckets: Array<{
    key: string
    label: string
    billed_cad: number
    net_billed_cad: number
    credits_cad: number
    app_actual_cad: number
    app_estimated_cad: number
    untracked_cad: number
    coverage_pct: number | null
  }>
  service_rows: ReconciliationServiceRow[]
  sku_rows: Array<{
    day: string
    service: string
    sku_id: string | null
    sku_description: string | null
    billed_cad: number
    net_billed_cad: number
    billing_rows: number
  }>
  top_untracked_services: ReconciliationServiceRow[]
  lag_note: string
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(usd: number) {
  return usd < 0.01 && usd > 0 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`
}

/** Whole dollars (no cents) — Budget settings daily/monthly limit readout. */
function fmtUsdWhole(usd: number) {
  return `$${Math.round(Number(usd) || 0)}`
}

/** Admin “By call type” table — fixed 4 decimals for aligned column. */
function fmtUsd4(usd: number) {
  return `$${usd.toFixed(4)}`
}

const CALL_TYPE_LABELS: Record<string, string> = {
  chat_phase0: 'Chat Query Analysis',
  chat_phase1: 'Chat Tool Routing',
  chat_phase2: 'Chat Response Generation',
  ocr_image: 'Ticket Attachment OCR',
  drive_ocr: 'Drive Document OCR',
  embed_ticket: 'Ticket Embeddings',
  embed_query: 'Search Query Embeddings',
  embed_drive: 'Drive Document Embeddings',
  analytics_sql: 'Analytics SQL Generation',
  analytics_decision: 'Analytics Routing',
  analytics_answer: 'Analytics Answer Generation',
  summary: 'Ticket Summary Generation',
  docs_gap: 'Knowledge Gap Analysis',
  vertex_search: 'Knowledge Search',
  cloud_run_idle: 'Cloud Run Idle',
  cloud_build: 'Cloud Build Deploy',
  universal_semantic: 'Universal Semantic Analysis',
  universal_p1: 'Universal Routing',
  universal_p2: 'Universal Response',
  universal_translate: 'Universal Translation',
  universal_review: 'Universal Answer Review',
  customer_risk: 'Customer Risk Analysis',
}

function titleCaseCallType(type: string): string {
  const acronyms: Record<string, string> = {
    api: 'API',
    bq: 'BQ',
    kb: 'KB',
    ocr: 'OCR',
    sql: 'SQL',
    ui: 'UI',
  }
  return type
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => acronyms[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ')
}

function fmtCallType(type: string): string {
  return CALL_TYPE_LABELS[type] ?? titleCaseCallType(type)
}

function cadAmount(value: { cost_cad?: number; cost_usd: number }) {
  return value.cost_cad ?? value.cost_usd
}

/** Format a Date as YYYY-MM-DDTHH:MM in Eastern timezone (for datetime-local inputs). */
function businessTimeStr(d: Date): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(d)
  const get = (t: string) => parts.find(p => p.type === t)?.value ?? '00'
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`
}

/** YYYY-MM-DDTHH:MM for start of today in Eastern. */
function businessTimeMidnight(): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const get = (t: string) => parts.find(p => p.type === t)?.value ?? '00'
  return `${get('year')}-${get('month')}-${get('day')}T00:00`
}

/** Today's date as YYYY-MM-DD in Eastern timezone */
function businessTimeToday(): string {
  return businessTimeMidnight().slice(0, 10)
}

// ---------------------------------------------------------------------------
// Draggable widget shell + grid layout helpers
// ---------------------------------------------------------------------------

const LAYOUT_KEY = 'esip_costs_layout_v19'

/**
 * 48-col grid. Top row: **same `h` for all four** (matches Sources & range); By-type list scrolls inside.
 * Widths unchanged (9+9+14+16). Chart row starts immediately below top row.
 */
const COSTS_GRID_COLS = 48
/** Grid row units for the whole top band — tied to right card (Sources & range) content height. */
/** Top-row grid height (units); 15 fits Budget card (subtitle + 5 rows + Edit) without clipping at rowHeight 13 */
const TOP_ROW_H = 15
const DEFAULT_LAYOUT: LayoutItem[] = [
  { i: 'today',    x: 0,  y: 0, w: 9,  h: TOP_ROW_H, minW: 7,  minH: 8 },
  { i: 'budget',   x: 9,  y: 0, w: 9,  h: TOP_ROW_H, minW: 7,  minH: 9 },
  { i: 'by-type',  x: 18, y: 0, w: 14, h: TOP_ROW_H, minW: 9,  minH: 8 },
  { i: 'controls', x: 32, y: 0, w: 16, h: TOP_ROW_H, minW: 10, minH: 9 },
  { i: 'chart',    x: 0,  y: TOP_ROW_H, w: 32, h: 14, minW: 24, minH: 8 },
  { i: 'coverage', x: 32, y: TOP_ROW_H, w: 16, h: 14, minW: 12, minH: 8 },
]

function loadLayout(): LayoutItem[] {
  try {
    const saved = localStorage.getItem(LAYOUT_KEY)
    return saved ? JSON.parse(saved) : DEFAULT_LAYOUT
  } catch { return DEFAULT_LAYOUT }
}
function saveLayout(layout: Layout) {
  localStorage.setItem(LAYOUT_KEY, JSON.stringify([...layout]))
}

type WidgetShellVariant = 'fill' | 'compact'

/**
 * `compact` — same full-height card as grid peers (aligned bottom borders); body scrolls if needed;
 * content wrapped in `shrink-0` so short cards do not stretch inner blocks, only the padded area grows.
 * `fill` — stretch + scroll body for long lists (by-type, chart).
 */
function WidgetShell({
  title,
  children,
  variant = 'fill',
}: {
  title: string
  children: React.ReactNode
  variant?: WidgetShellVariant
}) {
  const header = (
    <div className="drag-handle flex items-center gap-1.5 px-3 py-1 border-b bg-muted/40 cursor-grab active:cursor-grabbing select-none shrink-0">
      <GripHorizontal className="size-3.5 text-muted-foreground/60" />
      <span className="text-sm text-muted-foreground font-medium">{title}</span>
    </div>
  )
  if (variant === 'compact') {
    return (
      <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border bg-card">
        {header}
        <div className="flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto px-3 py-2">
          <div className="shrink-0 min-w-0">{children}</div>
        </div>
      </div>
    )
  }
  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden rounded-lg border bg-card">
      {header}
      <div className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto px-3 py-2.5">{children}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Cost alert banner
// ---------------------------------------------------------------------------

function CostAlertBanner({
  alert,
  onClear,
}: {
  alert: NonNullable<CostSnapshot['alert']>
  onClear: () => void
}) {
  const isHard = alert.hard_cap_active
  return (
    <div
      className={`rounded-lg border px-4 py-3 flex gap-3 items-start ${
        isHard
          ? 'border-destructive/50 bg-destructive/10'
          : 'border-yellow-500/50 bg-yellow-500/10'
      }`}
    >
      <AlertTriangle
        className={`size-5 mt-0.5 shrink-0 ${isHard ? 'text-destructive' : 'text-yellow-600'}`}
      />
      <div className="flex-1 min-w-0">
        <p className={`text-sm font-semibold ${isHard ? 'text-destructive' : 'text-yellow-700'}`}>
          {isHard ? 'Hard cap reached — platform blocked' : 'Soft cap reached'}
        </p>
        <p className="text-sm text-muted-foreground mt-0.5 line-clamp-2">{alert.message}</p>
        <p className="text-sm text-muted-foreground mt-0.5">{alert.at}</p>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="shrink-0 h-8 px-2 text-sm"
        onClick={onClear}
      >
        Clear
      </Button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Progress bar
// ---------------------------------------------------------------------------

function CostBar({ pct, softPct, hardPct }: { pct: number; softPct: number; hardPct: number }) {
  const clamped = Math.min(pct, 100)
  const color =
    pct >= hardPct ? 'bg-destructive' : pct >= softPct ? 'bg-yellow-500' : 'bg-primary'
  return (
    <div className="relative w-full h-2.5 rounded-full bg-muted overflow-hidden">
      {/* soft cap marker */}
      <div
        className="absolute top-0 bottom-0 w-px bg-yellow-400/70"
        style={{ left: `${softPct}%` }}
      />
      {/* hard cap marker */}
      <div
        className="absolute top-0 bottom-0 w-px bg-destructive/70"
        style={{ left: `${Math.min(hardPct, 99)}%` }}
      />
      <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${clamped}%` }} />
    </div>
  )
}


// ---------------------------------------------------------------------------
// Range selector (datetime-local, hourly + daily presets)
// ---------------------------------------------------------------------------

/** How BQ billing (daily export) relates to the preset vs Estimated (can be hourly for short ranges). */
type PresetBillingHint = 'subdaily_est' | 'today_mixed' | 'daily_both'

const RANGE_PRESETS: {
  label: string
  billingHint: PresetBillingHint
  title: string
  getFrom: () => string
}[] = [
  {
    label: '1h',
    billingHint: 'subdaily_est',
    title: 'Billed: daily · Estimated: sub-day buckets',
    getFrom: () => businessTimeStr(new Date(Date.now() - 1 * 3600_000)),
  },
  {
    label: '4h',
    billingHint: 'subdaily_est',
    title: 'Billed: daily · Estimated: sub-day buckets',
    getFrom: () => businessTimeStr(new Date(Date.now() - 4 * 3600_000)),
  },
  {
    label: '12h',
    billingHint: 'subdaily_est',
    title: 'Billed: daily · Estimated: sub-day buckets',
    getFrom: () => businessTimeStr(new Date(Date.now() - 12 * 3600_000)),
  },
  {
    label: 'Today',
    billingHint: 'today_mixed',
    title: 'Billed: calendar day · Estimated: midnight → now',
    getFrom: () => businessTimeMidnight(),
  },
  {
    label: '7d',
    billingHint: 'daily_both',
    title: 'Billed & estimated: daily totals',
    getFrom: () => businessTimeStr(new Date(Date.now() - 7 * 86400_000)),
  },
  {
    label: '30d',
    billingHint: 'daily_both',
    title: 'Billed & estimated: daily totals',
    getFrom: () => businessTimeStr(new Date(Date.now() - 30 * 86400_000)),
  },
]

/** Sharp 50% blue-100 / 50% emerald-100 (billed vs estimated), for pill buttons */
const PRESET_SPLIT_INACTIVE =
  'bg-[linear-gradient(90deg,rgb(219_234_254)_50%,rgb(209_250_229)_50%)] border-blue-200/90 border-r-emerald-300/90 text-foreground hover:brightness-[0.97]'

function presetPillClass(active: boolean, hint: PresetBillingHint): string {
  const base =
    'shrink-0 px-2.5 py-1 text-sm rounded-full border transition-colors inline-flex items-center justify-center whitespace-nowrap'
  if (active) {
    return `${base} bg-primary text-primary-foreground border-primary shadow-sm`
  }
  switch (hint) {
    case 'subdaily_est':
      return `${base} bg-emerald-50 border-emerald-300 text-emerald-950 hover:bg-emerald-100/90`
    case 'today_mixed':
      return `${base} ${PRESET_SPLIT_INACTIVE}`
    case 'daily_both':
      return `${base} ${PRESET_SPLIT_INACTIVE}`
    default:
      return `${base} bg-muted hover:bg-accent border-transparent`
  }
}

function RangeSelector({ from, to, activePreset, onPreset, onFromChange, onToChange, trailing }: {
  from: string
  to: string
  activePreset: string
  onPreset: (label: string, from: string, to: string) => void
  onFromChange: (v: string) => void
  onToChange: (v: string) => void
  /** e.g. Reset — immediately right of the date inputs (row not full card width) */
  trailing?: ReactNode
}) {
  const dtClass =
    [
      'box-border w-full min-w-[10.25rem] max-w-[11.25rem] border rounded px-1.5 py-1.5 text-sm tabular-nums',
      'bg-background text-foreground min-h-9',
      /* Pull native picker icon left (~half the default gap) — WebKit/Blink */
      '[&::-webkit-calendar-picker-indicator]:mr-0 [&::-webkit-calendar-picker-indicator]:-translate-x-1',
      '[&::-webkit-calendar-picker-indicator]:cursor-pointer [&::-webkit-calendar-picker-indicator]:opacity-90',
    ].join(' ')
  const labelCls = 'text-sm text-muted-foreground text-left leading-none self-center shrink-0 w-9'
  return (
    <div className="space-y-2">
      <div className="flex flex-nowrap items-center gap-1.5 overflow-x-auto pb-0.5 [-webkit-overflow-scrolling:touch]">
        {RANGE_PRESETS.map(({ label, getFrom, billingHint, title }) => (
          <button
            type="button"
            key={label}
            title={title}
            onClick={() => {
              const f = getFrom()
              const t = businessTimeStr(new Date())
              onPreset(label, f, t)
            }}
            className={presetPillClass(activePreset === label, billingHint)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="inline-flex max-w-full flex-row flex-wrap items-start gap-1.5 min-w-0">
        <div
          className="grid shrink-0 items-center gap-x-1.5 gap-y-1.5"
          style={{ gridTemplateColumns: '2.25rem minmax(10.25rem,11.25rem)' }}
        >
          <span className={labelCls}>from</span>
          <input
            type="datetime-local"
            value={from}
            onChange={e => onFromChange(e.target.value)}
            className={dtClass}
          />
          <span className={labelCls}>to</span>
          <input
            type="datetime-local"
            value={to}
            onChange={e => onToChange(e.target.value)}
            className={dtClass}
          />
        </div>
        {trailing ? <div className="shrink-0 self-center pt-0.5">{trailing}</div> : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Costs tab
// ---------------------------------------------------------------------------

/** Costs body: uniform readable `text-sm` (shadcn default feel); titles in shell header. */
function CostsTab() {
  // ── Snapshot (today + budget + alert) ────────────────────────────────────
  const [snapshot, setSnapshot] = useState<CostSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [, setClearing] = useState(false)

  // ── Source toggles ────────────────────────────────────────────────────────
  const [showEstimated, setShowEstimated] = useState(true)
  const [showBilled, setShowBilled]       = useState(true)

  // ── Unified range (datetime-local string, same format as old RangeSelector) ─
  const [rangeFrom, setRangeFrom] = useState(() => businessTimeStr(new Date(Date.now() - 7 * 86400_000)))
  const [rangeTo, setRangeTo]     = useState(() => businessTimeStr(new Date()))
  const [activePreset, setActivePreset] = useState('7d')

  // ── Firestore range data ──────────────────────────────────────────────────
  const [rangeData, setRangeData]     = useState<RangeData | null>(null)
  const [rangeLoading, setRangeLoading] = useState(false)

  // ── BQ Billing data ───────────────────────────────────────────────────────
  const [billingData, setBillingData]   = useState<BillingData | null>(null)
  const [billingLoading, setBillingLoading] = useState(false)
  const [billingError, setBillingError] = useState<string | null>(null)

  // ── Billing reconciliation / coverage data ───────────────────────────────
  const [reconData, setReconData] = useState<ReconciliationData | null>(null)
  const [reconLoading, setReconLoading] = useState(false)
  const [reconError, setReconError] = useState<string | null>(null)

  // ── Budget edit ───────────────────────────────────────────────────────────
  const [editing, setEditing]   = useState(false)
  const [budgetForm, setBudgetForm] = useState<Record<string, number>>({})
  const [saving, setSaving]     = useState(false)
  const [saveMsg, setSaveMsg]   = useState<string | null>(null)

  type ByTypeSortKey = 'name' | 'calls' | 'cost'
  const [byTypeSort, setByTypeSort] = useState<{ key: ByTypeSortKey; asc: boolean }>({ key: 'cost', asc: false })

  // ── Grid layout — MUST be before any early returns (Rules of Hooks) ───────
  const [gridLayout, setGridLayout] = useState<LayoutItem[]>(loadLayout)
  const { containerRef: gridRef, width: gridWidth } = useContainerWidth({ initialWidth: 1200, measureBeforeMount: false })

  // ── Data loaders ──────────────────────────────────────────────────────────
  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    apiFetch<CostSnapshot>('/api/admin/cost/snapshot')
      .then(setSnapshot)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false))
  }, [])

  const loadRange = useCallback((f: string, t: string) => {
    setRangeLoading(true)
    apiFetch<RangeData>(
      `/api/admin/cost/range?from_dt=${encodeURIComponent(f)}&to_dt=${encodeURIComponent(t)}`
    )
      .then(setRangeData)
      .catch((e) => console.error('range load failed', e))
      .finally(() => setRangeLoading(false))
  }, [])

  const loadBilling = useCallback((f: string, t: string) => {
    setBillingLoading(true)
    setBillingError(null)
    // billing endpoint expects date-only (YYYY-MM-DD)
    apiFetch<BillingData>(
      `/api/admin/cost/billing?from_dt=${encodeURIComponent(f.slice(0, 10))}&to_dt=${encodeURIComponent(t.slice(0, 10))}`
    )
      .then(setBillingData)
      .catch((e) => setBillingError((e as Error).message))
      .finally(() => setBillingLoading(false))
  }, [])

  const loadReconciliation = useCallback((f: string, t: string) => {
    setReconLoading(true)
    setReconError(null)
    apiFetch<ReconciliationData>(
      `/api/admin/cost/reconciliation?from_dt=${encodeURIComponent(f.slice(0, 10))}&to_dt=${encodeURIComponent(t.slice(0, 10))}`
    )
      .then(setReconData)
      .catch((e) => setReconError((e as Error).message))
      .finally(() => setReconLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  // Initial chart/range load only — preset clicks and Refresh fetch explicitly.
  useEffect(() => {
    loadRange(rangeFrom, rangeTo)
    loadBilling(rangeFrom, rangeTo)
    loadReconciliation(rangeFrom, rangeTo)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only with initial Eastern range
  }, [loadRange, loadBilling, loadReconciliation])

  function applyRange(label: string, f: string, t: string) {
    setRangeFrom(f)
    setRangeTo(t)
    setActivePreset(label)
    loadRange(f, t)
    loadBilling(f, t)
    loadReconciliation(f, t)
  }

  const onRangeFromInput = useCallback((v: string) => {
    setRangeFrom(v)
    setActivePreset('')
  }, [])

  const onRangeToInput = useCallback((v: string) => {
    setRangeTo(v)
    setActivePreset('')
  }, [])

  const sortedByTypeEntries = useMemo(() => {
    if (!rangeData?.by_type) return []
    const entries = Object.entries(rangeData.by_type)
    const { key, asc } = byTypeSort
    return [...entries].sort(([ta, a], [tb, b]) => {
      let cmp = 0
      if (key === 'name') {
        cmp = fmtCallType(ta).localeCompare(fmtCallType(tb), undefined, { sensitivity: 'base' })
      } else if (key === 'calls') {
        cmp = a.calls - b.calls
      } else {
        cmp = cadAmount(a) - cadAmount(b)
      }
      return asc ? cmp : -cmp
    })
  }, [rangeData?.by_type, byTypeSort])

  function headerByTypeSort(col: ByTypeSortKey) {
    setByTypeSort((prev) =>
      prev.key === col ? { key: col, asc: !prev.asc } : { key: col, asc: col === 'name' }
    )
  }

  function startEdit() {
    if (!snapshot) return
    setBudgetForm({ ...snapshot.budget })
    setEditing(true)
    setSaveMsg(null)
  }

  async function saveBudget() {
    setSaving(true)
    setSaveMsg(null)
    try {
      await apiFetch('/api/admin/cost/budget', {
        method: 'PATCH',
        body: JSON.stringify(budgetForm),
      })
      setSaveMsg('Saved')
      setEditing(false)
      load()
    } catch (e) {
      setSaveMsg((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  async function clearAlert() {
    setClearing(true)
    try {
      await apiFetch('/api/admin/cost/alerts/clear', { method: 'POST' })
      load()
    } finally {
      setClearing(false)
    }
  }

  // ── Early returns AFTER all hooks ─────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-8 text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading cost data…
      </div>
    )
  }
  if (error) {
    return (
      <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
        {error}
      </div>
    )
  }
  if (!snapshot) return null

  const { today, budget, alert } = snapshot
  const softPct = budget.soft_cap_pct ?? 70
  const hardPct = budget.hard_cap_pct ?? 90

  // ── Build unified chart data ──────────────────────────────────────────────
  const estimatedMap: Record<string, number> = {}
  if (rangeData) {
    for (const b of rangeData.buckets) {
      const dayKey = b.key.slice(0, 10)
      estimatedMap[dayKey] = (estimatedMap[dayKey] ?? 0) + cadAmount(b)
    }
  }
  const billingMap: Record<string, number> = {}
  if (billingData) {
    for (const b of billingData.buckets) {
      billingMap[b.key] = cadAmount(b)
    }
  }
  const allKeys = Array.from(
    new Set([
      ...Object.keys(estimatedMap),
      ...Object.keys(billingMap),
      ...(reconData?.buckets.map(b => b.key) ?? []),
    ])
  ).sort()
  const untrackedMap: Record<string, number> = {}
  if (reconData) {
    for (const b of reconData.buckets) {
      untrackedMap[b.key] = b.untracked_cad
    }
  }
  const chartData = allKeys.map(k => ({
    label: k.slice(5),
    estimated: estimatedMap[k] ?? 0,
    billed: billingMap[k] ?? 0,
    untracked: untrackedMap[k] ?? 0,
  }))

  const todayBilledBucket = billingData?.buckets.find(b => b.key === businessTimeToday()) ?? null
  const todayBilled = todayBilledBucket ? cadAmount(todayBilledBucket) : null

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-2">
      {/* Alert banner */}
      {alert && <CostAlertBanner alert={alert} onClear={clearAlert} />}

      {/* Draggable widgets grid — ALL content is inside */}
      <div ref={gridRef}>
        <div className="flex justify-end items-center mb-2">
          <Button
            variant="outline" size="sm"
            onClick={() => { setGridLayout(DEFAULT_LAYOUT); saveLayout(DEFAULT_LAYOUT) }}
            className="h-6 px-2 text-xs text-orange-400 border-orange-200 hover:text-orange-500 hover:border-orange-300 hover:bg-orange-50/50"
          >
            ↺ Layout Reset
          </Button>
        </div>
        <GridLayout
          layout={gridLayout}
          width={gridWidth}
          gridConfig={{
            cols: COSTS_GRID_COLS,
            rowHeight: 13,
            margin: [8, 8],
            containerPadding: [0, 0],
          }}
          dragConfig={{ handle: '.drag-handle' }}
          resizeConfig={{ handles: ['se', 'sw', 'ne', 'nw'] }}
          onLayoutChange={(l: Layout) => { setGridLayout([...l]); saveLayout(l) }}
        >
          {/* Top row: Today (narrow) | Budget (narrow) | By type (medium) | Sources & range (wide) */}
          <div key="today">
            <WidgetShell title="Today" variant="compact">
              <div className="space-y-2 text-sm">
                <p className="text-muted-foreground leading-none">{today.date}</p>
                <div className="leading-snug space-y-0.5">
                  <p className="text-blue-700 font-medium">Billed: GCP calendar day</p>
                  <p className="text-emerald-700 font-medium">Est.: meter to now</p>
                </div>
                {showBilled && (
                  <div className="flex items-center gap-2">
                    <span className="size-2 shrink-0 rounded-full bg-blue-500" />
                    {billingLoading
                      ? <Loader2 className="size-4 animate-spin text-blue-500" />
                      : <span className="text-lg font-bold tabular-nums text-blue-600 leading-none">{todayBilled !== null ? fmt(todayBilled) : '—'}</span>
                    }
                    <span className="text-muted-foreground">billed</span>
                  </div>
                )}
                {showEstimated && (
                  <div className="flex items-center gap-2">
                    <span className="size-2 shrink-0 rounded-full bg-emerald-500" />
                    <span className="text-lg font-bold tabular-nums text-emerald-600 leading-none">{fmt(cadAmount(today))}</span>
                    <span className="text-muted-foreground">{today.calls} calls · est.</span>
                  </div>
                )}
                <p className="text-muted-foreground leading-snug">{today.pct_of_daily_limit.toFixed(1)}% of {fmt(budget.daily_limit_cad ?? budget.daily_limit_usd)}/day</p>
                <CostBar pct={today.pct_of_daily_limit} softPct={softPct} hardPct={hardPct} />
                <div className="flex gap-3 text-muted-foreground leading-none">
                  <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-yellow-400" />Soft {softPct}%</span>
                  <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-destructive" />Hard {hardPct}%</span>
                </div>
              </div>
            </WidgetShell>
          </div>

          <div key="budget">
            <WidgetShell title="Budget settings" variant="compact">
              {/* Tighter than Today: gap = space-y-2 / 2.25 (÷1.5 twice) between subtitle, rows, footer */}
              <div className="flex flex-col text-sm [gap:calc(0.5rem/2.25)]">
                <p className="text-muted-foreground leading-none">In-app caps & alerts</p>
                {(
                  [
                    {
                      key: 'daily_limit_usd',
                      label: 'Daily limit',
                      read: () => fmtUsdWhole(budget.daily_limit_cad ?? budget.daily_limit_usd),
                      step: '0.01',
                      suffix: null,
                      valueSlot: 'money' as const,
                    },
                    {
                      key: 'monthly_limit_usd',
                      label: 'Monthly limit',
                      read: () => fmtUsdWhole(budget.monthly_limit_cad ?? budget.monthly_limit_usd),
                      step: '0.01',
                      suffix: null,
                      valueSlot: 'money' as const,
                    },
                    {
                      key: 'soft_cap_pct',
                      label: 'Soft cap',
                      read: () => `${budget.soft_cap_pct}%`,
                      step: '1',
                      suffix: 'pct' as const,
                      valueSlot: 'short' as const,
                    },
                    {
                      key: 'hard_cap_pct',
                      label: 'Hard cap',
                      read: () => `${budget.hard_cap_pct}%`,
                      step: '1',
                      suffix: 'pct' as const,
                      valueSlot: 'short' as const,
                    },
                    {
                      key: 'divergence_threshold',
                      label: 'Divergence',
                      read: () => `${budget.divergence_threshold}×`,
                      step: '0.1',
                      suffix: 'times' as const,
                      valueSlot: 'short' as const,
                    },
                  ] as const
                ).map(({ key, label, read, step, suffix, valueSlot }) => {
                  /* One width for every row: prefix ($, %, ×) + 4.35rem number box — same order as money */
                  const slotClass = 'w-[6rem] min-w-[6rem]'
                  const shortReadBody =
                    suffix === 'pct'
                      ? read().replace(/%$/, '')
                      : suffix === 'times'
                        ? read().replace(/×$/, '')
                        : read()
                  return (
                  <div
                    key={key}
                    className="flex min-w-0 items-center gap-2 leading-none"
                  >
                    <span className="min-w-0 flex-1 pr-2 text-muted-foreground break-words">{label}</span>
                    <div
                      className={`flex h-8 shrink-0 items-center justify-end gap-0.5 ${slotClass}`}
                    >
                      {!editing ? (
                        valueSlot === 'money' ? (
                          <>
                            <span className="shrink-0 font-semibold tabular-nums text-muted-foreground" aria-hidden>$</span>
                            <span className="inline-flex h-8 w-[4.35rem] items-center justify-end truncate text-sm font-semibold tabular-nums text-foreground">
                              {read().replace(/^\$\s*/, '')}
                            </span>
                          </>
                        ) : (
                          <>
                            {suffix === 'pct' && (
                              <span className="shrink-0 font-semibold tabular-nums text-muted-foreground" aria-hidden>%</span>
                            )}
                            {suffix === 'times' && (
                              <span className="shrink-0 font-semibold tabular-nums text-muted-foreground" aria-hidden>×</span>
                            )}
                            <span className="inline-flex h-8 w-[4.35rem] items-center justify-end truncate text-right text-sm font-semibold tabular-nums text-foreground">
                              {shortReadBody}
                            </span>
                          </>
                        )
                      ) : (
                        <>
                          {(key === 'daily_limit_usd' || key === 'monthly_limit_usd') && (
                            <span className="shrink-0 font-semibold tabular-nums text-muted-foreground" aria-hidden>$</span>
                          )}
                          {suffix === 'pct' && (
                            <span className="shrink-0 font-semibold tabular-nums text-muted-foreground" aria-hidden>%</span>
                          )}
                          {suffix === 'times' && (
                            <span className="shrink-0 font-semibold tabular-nums text-muted-foreground" aria-hidden>×</span>
                          )}
                          <Input
                            type="number"
                            step={step}
                            inputMode="decimal"
                            aria-label={label}
                            className="h-8 w-[4.35rem] shrink-0 border-muted-foreground/25 bg-background px-1.5 py-0 text-right text-sm font-semibold tabular-nums shadow-none focus-visible:ring-1 md:text-sm"
                            value={Number.isFinite(budgetForm[key]) ? String(budgetForm[key]) : ''}
                            onChange={(e) => {
                              const raw = e.target.value
                              if (raw === '' || raw === '-') return
                              const v = parseFloat(raw)
                              if (Number.isFinite(v)) {
                                setBudgetForm((f) => ({ ...f, [key]: v }))
                              }
                            }}
                          />
                        </>
                      )}
                    </div>
                  </div>
                  )
                })}
                <div className="border-t border-border/60 pt-[calc(0.625rem/2.25)]">
                  {!editing ? (
                    <Button variant="outline" size="sm" onClick={startEdit} className="h-8 w-full text-sm">
                      Edit
                    </Button>
                  ) : (
                    <div className="flex flex-col gap-1">
                      <div className="flex h-8 items-center gap-2">
                        <Button size="sm" className="h-8 flex-1 px-2 text-sm" onClick={saveBudget} disabled={saving}>
                          {saving ? <Loader2 className="mr-1 size-3 animate-spin" /> : null}
                          Save
                        </Button>
                        <Button variant="outline" size="sm" className="h-8 flex-1 px-2 text-sm" type="button" onClick={() => setEditing(false)}>
                          Cancel
                        </Button>
                      </div>
                      {saveMsg && (
                        <span className={`text-xs leading-tight ${saveMsg === 'Saved' ? 'text-green-600' : 'text-destructive'}`}>{saveMsg}</span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </WidgetShell>
          </div>

          <div key="by-type">
            <WidgetShell title="By cost area (estimated)">
              {!showEstimated ? (
                <p className="text-xs text-muted-foreground">Enable Estimated source to see breakdown.</p>
              ) : !rangeData || Object.keys(rangeData.by_type).length === 0 ? (
                <p className="text-xs text-muted-foreground">No data for selected range.</p>
              ) : (
                <div className="min-w-0 divide-y text-xs">
                  <div
                    className="grid min-w-0 w-full grid-cols-[minmax(0,1fr)_max-content_auto_max-content] items-center gap-x-1.5 pb-1.5 mb-0.5 border-b border-border font-medium text-muted-foreground"
                  >
                    <button
                      type="button"
                      onClick={() => headerByTypeSort('name')}
                      className="min-w-0 text-left rounded px-0.5 -mx-0.5 py-0.5 hover:bg-muted/70 hover:text-foreground flex items-center gap-0.5"
                    >
                      <span>Cost area</span>
                      {byTypeSort.key === 'name'
                        ? (byTypeSort.asc
                            ? <ChevronUp className="size-3 shrink-0 opacity-90" aria-hidden />
                            : <ChevronDown className="size-3 shrink-0 opacity-90" aria-hidden />)
                        : null}
                    </button>
                    <button
                      type="button"
                      onClick={() => headerByTypeSort('calls')}
                      className="shrink-0 whitespace-nowrap text-right rounded px-0.5 -mx-0.5 py-0.5 hover:bg-muted/70 hover:text-foreground flex items-center justify-end gap-0.5 tabular-nums"
                    >
                      <span>Calls</span>
                      {byTypeSort.key === 'calls'
                        ? (byTypeSort.asc
                            ? <ChevronUp className="size-3 shrink-0 opacity-90" aria-hidden />
                            : <ChevronDown className="size-3 shrink-0 opacity-90" aria-hidden />)
                        : null}
                    </button>
                    <span className="shrink-0 whitespace-nowrap text-center text-muted-foreground/50 select-none" aria-hidden>|</span>
                    <button
                      type="button"
                      onClick={() => headerByTypeSort('cost')}
                      className="shrink-0 whitespace-nowrap text-right rounded px-0.5 -mx-0.5 py-0.5 hover:bg-muted/70 hover:text-foreground flex items-center justify-end gap-0.5 tabular-nums"
                    >
                      <span>Est. (CAD)</span>
                      {byTypeSort.key === 'cost'
                        ? (byTypeSort.asc
                            ? <ChevronUp className="size-3 shrink-0 opacity-90" aria-hidden />
                            : <ChevronDown className="size-3 shrink-0 opacity-90" aria-hidden />)
                        : null}
                    </button>
                  </div>
                  {sortedByTypeEntries.map(([type, data]) => (
                    <div
                      key={type}
                      className="grid min-w-0 w-full grid-cols-[minmax(0,1fr)_max-content_auto_max-content] items-center gap-x-1.5 py-1.5"
                    >
                      <span className="min-w-0 break-words font-medium leading-snug text-foreground">
                        {fmtCallType(type)}
                      </span>
                      <span className="shrink-0 whitespace-nowrap text-right tabular-nums text-muted-foreground">
                        {data.calls} calls
                      </span>
                      <span className="shrink-0 whitespace-nowrap text-center text-muted-foreground/50 tabular-nums">
                        |
                      </span>
                      <span className="shrink-0 whitespace-nowrap text-right tabular-nums font-semibold text-foreground">
                        {fmtUsd4(cadAmount(data))}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </WidgetShell>
          </div>

          <div key="controls">
            <WidgetShell title="Sources & range" variant="compact">
              <div className="flex flex-col gap-2 text-sm">
                <div className="flex flex-col gap-1.5">
                  <p className="font-medium text-muted-foreground">Data sources (click to choose)</p>
                  <div className="text-left leading-snug text-muted-foreground">
                    <p>
                      <span className="font-medium text-blue-600">Billed</span>
                      <span> = </span>
                      <span className="text-blue-600">full GCP spend</span>
                    </p>
                    <p>
                      <span className="font-medium text-emerald-600">App-metered</span>
                      <span> = </span>
                      <span className="text-emerald-600">runtime guard data</span>
                    </p>
                  </div>
                  <div className="flex flex-nowrap gap-1.5">
                    <button
                      type="button"
                      onClick={() => setShowBilled(v => !v)}
                      className={`inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-medium transition-all ${
                        showBilled
                          ? 'bg-blue-50 border-blue-300 text-blue-700'
                          : 'bg-muted border-transparent text-muted-foreground'
                      }`}
                    >
                      <span className={`size-2 rounded-full ${showBilled ? 'bg-blue-500' : 'bg-muted-foreground/40'}`} />
                      Billed (BQ, 1–24h lag)
                    </button>
                    <button
                      type="button"
                      onClick={() => setShowEstimated(v => !v)}
                      className={`inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-medium transition-all ${
                        showEstimated
                          ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                          : 'bg-muted border-transparent text-muted-foreground'
                      }`}
                    >
                      <span className={`size-2 rounded-full ${showEstimated ? 'bg-emerald-500' : 'bg-muted-foreground/40'}`} />
                      App-metered estimate
                    </button>
                  </div>
                </div>
                <div className="w-fit max-w-full min-w-0">
                  <RangeSelector
                    from={rangeFrom}
                    to={rangeTo}
                    activePreset={activePreset}
                    onPreset={applyRange}
                    onFromChange={onRangeFromInput}
                    onToChange={onRangeToInput}
                    trailing={(
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        title="Reload snapshot and chart for the custom range above (presets load immediately)"
                        onClick={() => {
                          load()
                          loadRange(rangeFrom, rangeTo)
                          loadBilling(rangeFrom, rangeTo)
                          loadReconciliation(rangeFrom, rangeTo)
                        }}
                        className="h-9 shrink-0 px-2.5 text-sm font-medium"
                      >
                        Set
                      </Button>
                    )}
                  />
                </div>
              </div>
            </WidgetShell>
          </div>

          <div key="chart">
            <WidgetShell title="Cost history">
              <div className="h-full flex flex-col gap-2 min-h-0 text-sm">
                <p className="shrink-0 text-sm leading-snug text-muted-foreground">
                  <span className="font-medium text-blue-600">Blue</span>
                  {' = '}
                  <span className="text-blue-600">billed</span>
                  <span> · </span>
                  <span className="font-medium text-emerald-600">Green</span>
                  {' = '}
                  <span className="text-emerald-600">app-metered</span>
                  <span> · </span>
                  <span className="font-medium text-orange-600">Orange</span>
                  {' = '}
                  <span className="text-orange-600">untracked billed</span>
                </p>
                {(rangeLoading || billingLoading) && (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" /> Loading…
                  </div>
                )}
                {billingError && (
                  <div className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-sm text-destructive">
                    Billing: {billingError}
                  </div>
                )}
                {reconError && (
                  <div className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-sm text-destructive">
                    Reconciliation: {reconError}
                  </div>
                )}
                <div className="flex flex-wrap gap-4">
                  {showBilled && (
                    <div className="flex items-center gap-1.5">
                      <span className="size-2 rounded-full bg-blue-500" />
                      <span className="font-bold tabular-nums text-blue-600">{billingLoading ? '…' : fmt(billingData?.total_cost_cad ?? billingData?.total_cost_usd ?? 0)}</span>
                      {billingData?.latest_day && <span className="text-muted-foreground">billed · {billingData.latest_day}</span>}
                    </div>
                  )}
                  {showEstimated && (
                    <div className="flex items-center gap-1.5">
                      <span className="size-2 rounded-full bg-emerald-500" />
                      <span className="font-bold tabular-nums text-emerald-600">{rangeLoading ? '…' : fmt(rangeData?.total_cost_cad ?? rangeData?.total_cost_usd ?? 0)}</span>
                      {rangeData && <span className="text-muted-foreground">{rangeData.total_calls} calls · est.</span>}
                    </div>
                  )}
                  {showBilled && reconData && (
                    <div className="flex items-center gap-1.5">
                      <span className="size-2 rounded-full bg-orange-500" />
                      <span className="font-bold tabular-nums text-orange-600">{reconLoading ? '…' : fmt(reconData.totals.untracked_cad)}</span>
                      <span className="text-muted-foreground">untracked billed</span>
                    </div>
                  )}
                </div>
                {!showEstimated && !showBilled ? (
                  <p className="text-muted-foreground">Enable at least one source above.</p>
                ) : (
                  <div className="flex-1 min-h-0">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={chartData} barCategoryGap="25%" barGap={2}>
                        <XAxis dataKey="label" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
                        <YAxis tickFormatter={(v) => fmt(Number(v))} tick={{ fontSize: 10 }} axisLine={false} tickLine={false} width={52} />
                        <Tooltip
                          formatter={(v, name) => [
                            fmt(Number(v ?? 0)),
                            name === 'estimated'
                              ? 'App-metered actual/estimate'
                              : name === 'untracked'
                                ? 'Untracked billed'
                                : 'Billed (BQ, daily)',
                          ]}
                          contentStyle={{ fontSize: 12 }}
                        />
                        <Legend
                          formatter={(v) =>
                            v === 'estimated'
                              ? 'App-metered'
                              : v === 'untracked'
                                ? 'Untracked billed'
                                : 'Billed (BQ, daily)'
                          }
                          wrapperStyle={{ fontSize: 11 }}
                        />
                        {showBilled    && <Bar dataKey="billed"    fill="#3b82f6" radius={[3, 3, 0, 0]} maxBarSize={24} />}
                        {showEstimated && <Bar dataKey="estimated" fill="#10b981" radius={[3, 3, 0, 0]} maxBarSize={24} />}
                        {showBilled && reconData && <Bar dataKey="untracked" fill="#f97316" radius={[3, 3, 0, 0]} maxBarSize={24} />}
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>
            </WidgetShell>
          </div>

          <div key="coverage">
            <WidgetShell title="Billing coverage">
              <div className="flex h-full min-h-0 flex-col gap-2 text-sm">
                <p className="shrink-0 text-xs leading-snug text-muted-foreground">
                  Full GCP billed cost vs app-metered runtime cost. Hard gates apply to app-metered controllable calls. Untracked is infrastructure or unmapped SKU spend.
                </p>
                {reconLoading && (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" /> Loading reconciliation…
                  </div>
                )}
                {reconError && (
                  <div className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
                    {reconError}
                  </div>
                )}
                {!reconData ? (
                  <p className="text-xs text-muted-foreground">No reconciliation data loaded.</p>
                ) : (
                  <>
                    <div className="grid grid-cols-2 gap-2">
                      <div className="rounded border bg-muted/30 px-2 py-1.5">
                        <p className="text-xs text-muted-foreground">Billed net</p>
                        <p className="font-bold tabular-nums text-blue-600">{fmt(reconData.totals.net_billed_cad)}</p>
                      </div>
                      <div className="rounded border bg-muted/30 px-2 py-1.5">
                        <p className="text-xs text-muted-foreground">App-metered</p>
                        <p className="font-bold tabular-nums text-emerald-600">{fmt(reconData.totals.app_actual_cad)}</p>
                      </div>
                      <div className="rounded border bg-muted/30 px-2 py-1.5">
                        <p className="text-xs text-muted-foreground">Untracked billed</p>
                        <p className="font-bold tabular-nums text-orange-600">{fmt(reconData.totals.untracked_cad)}</p>
                      </div>
                      <div className="rounded border bg-muted/30 px-2 py-1.5">
                        <p className="text-xs text-muted-foreground">Coverage</p>
                        <p className="font-bold tabular-nums text-foreground">
                          {reconData.totals.coverage_pct === null ? '—' : `${reconData.totals.coverage_pct.toFixed(1)}%`}
                        </p>
                      </div>
                    </div>
                    <div className="min-h-0 flex-1 overflow-auto rounded border">
                      <div className="sticky top-0 grid grid-cols-[minmax(0,1fr)_max-content_max-content] gap-2 border-b bg-card px-2 py-1 text-xs font-medium text-muted-foreground">
                        <span>Top untracked service</span>
                        <span className="text-right">Untracked</span>
                        <span className="text-right">Billed</span>
                      </div>
                      {reconData.top_untracked_services.length === 0 ? (
                        <p className="px-2 py-2 text-xs text-muted-foreground">No untracked billed services in range.</p>
                      ) : (
                        reconData.top_untracked_services.slice(0, 8).map((row) => (
                          <div
                            key={`${row.day}-${row.service}`}
                            className="grid grid-cols-[minmax(0,1fr)_max-content_max-content] gap-2 border-b px-2 py-1.5 text-xs last:border-b-0"
                            title={row.call_types ?? undefined}
                          >
                            <span className="min-w-0 break-words font-medium leading-snug">
                              {row.day.slice(5)} · {row.service}
                            </span>
                            <span className="text-right tabular-nums text-orange-600">{fmt(row.untracked_cad)}</span>
                            <span className="text-right tabular-nums text-muted-foreground">{fmt(row.net_billed_cad)}</span>
                          </div>
                        ))
                      )}
                    </div>
                  </>
                )}
              </div>
            </WidgetShell>
          </div>
        </GridLayout>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Notifications tab
// ---------------------------------------------------------------------------

function NotificationsTab() {
  const [recipients, setRecipients] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [newEmail, setNewEmail] = useState('')
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  function load() {
    setLoading(true)
    apiFetch<{ recipients: string[] }>('/api/admin/notifications')
      .then((r) => setRecipients(r.recipients))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  async function addRecipient() {
    const email = newEmail.trim().toLowerCase()
    if (!email || !email.includes('@')) return
    setAdding(true)
    try {
      await apiFetch('/api/admin/notifications/recipients', {
        method: 'POST',
        body: JSON.stringify({ email }),
      })
      setNewEmail('')
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setAdding(false)
    }
  }

  async function removeRecipient(email: string) {
    setRemoving(email)
    try {
      await apiFetch(`/api/admin/notifications/recipients/${encodeURIComponent(email)}`, {
        method: 'DELETE',
      })
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRemoving(null)
    }
  }

  async function sendTest() {
    setTesting(true)
    setTestMsg(null)
    try {
      const r = await apiFetch<{ sent: boolean; recipient: string; transport: string }>('/api/admin/notifications/test', {
        method: 'POST',
      })
      setTestMsg(r.sent
        ? `Test email sent to ${r.recipient}`
        : `Failed to send to ${r.recipient} via ${r.transport} — check the configured delivery transport`)
    } catch (e) {
      setTestMsg((e as Error).message)
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-5">
      {error && (
        <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="rounded-lg border bg-card">
        <div className="px-4 py-3 border-b flex items-center justify-between">
          <div>
            <p className="text-sm font-medium">Alert recipients</p>
            <p className="text-xs text-muted-foreground mt-0.5">
              Receive soft cap warnings and hard cap critical alerts. Test emails go only to a safe test mailbox.
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={sendTest}
            disabled={testing}
            className="gap-1.5 shrink-0"
          >
            {testing ? <Loader2 className="size-3 animate-spin" /> : <Bell className="size-3" />}
            Send test
          </Button>
        </div>

        {testMsg && (
          <div className={`px-4 py-2 text-xs flex items-center gap-1.5 border-b ${
            testMsg.includes('sent') ? 'text-green-600' : 'text-destructive'
          }`}>
            {testMsg.includes('sent')
              ? <CheckCircle2 className="size-3" />
              : <AlertTriangle className="size-3" />}
            {testMsg}
          </div>
        )}

        <div className="divide-y">
          {loading && (
            <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Loading…
            </div>
          )}
          {!loading && recipients.length === 0 && (
            <div className="px-4 py-3 text-sm text-muted-foreground">No recipients configured.</div>
          )}
          {recipients.map((email) => (
            <div key={email} className="flex items-center justify-between px-4 py-2.5">
              <span className="text-sm">{email}</span>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                disabled={removing === email}
                onClick={() => removeRecipient(email)}
              >
                {removing === email
                  ? <Loader2 className="size-3 animate-spin" />
                  : <X className="size-3" />}
              </Button>
            </div>
          ))}
        </div>

        <div className="px-4 py-3 border-t flex gap-2">
          <Input
            placeholder="morgan.chen@northstar.example.com"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void addRecipient() }}
            className="text-sm h-8"
          />
          <Button size="sm" className="h-8 gap-1" onClick={addRecipient} disabled={adding}>
            {adding ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
            Add
          </Button>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        The Reference Environment validates this workflow locally; connected environments use their configured email transport.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// What’s New / change announcements tab (Super Admin)
// ---------------------------------------------------------------------------

interface ChangeCandidate {
  id: string
  title: string
  summary: string
  change_type: string
  impact: string
  source: string
  related_files: string[]
  audience_roles?: string[]
  surfaces?: string[]
  severity?: string
  status: 'draft' | 'published' | 'ignored'
  created_at: string
  published_as_announcement_id?: string | null
}

interface ProductAnnouncement {
  id: string
  title: string
  body_markdown: string
  severity: string
  status?: 'published' | 'unpublished'
  audience_roles: string[]
  surfaces: string[]
  published_at: string
  published_by: string
  unpublished_at?: string | null
  unpublished_by?: string | null
  republished_at?: string | null
  republished_by?: string | null
  edited_at?: string | null
  edited_by?: string | null
}

interface AnnouncementStats {
  announcement: ProductAnnouncement
  read_count: number
  dismissed_count: number
  reads: Array<{ id: string; user_email: string; dismissed_at?: string | null }>
}

type AnnouncementAudienceRole = 'user' | 'admin'

interface AnnouncementEditState {
  title: string
  body: string
  severity: string
  audience_roles: AnnouncementAudienceRole[]
}

const DEFAULT_ANNOUNCEMENT_AUDIENCE: AnnouncementAudienceRole[] = ['user', 'admin']

function formatAdminDate(value: string | null | undefined) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function normalizeAnnouncementAudience(values: string[] | null | undefined): AnnouncementAudienceRole[] {
  const roles = new Set(values ?? [])
  const next: AnnouncementAudienceRole[] = []
  if (roles.has('all') || roles.has('user')) next.push('user')
  if (roles.has('all') || roles.has('admin')) next.push('admin')
  return next.length ? next : [...DEFAULT_ANNOUNCEMENT_AUDIENCE]
}

function toggleAnnouncementAudience(
  roles: AnnouncementAudienceRole[],
  role: AnnouncementAudienceRole,
  checked: boolean,
): AnnouncementAudienceRole[] {
  const next = new Set(roles)
  if (checked) {
    next.add(role)
  } else if (next.size > 1) {
    next.delete(role)
  }
  return DEFAULT_ANNOUNCEMENT_AUDIENCE.filter(item => next.has(item))
}

function announcementAudienceLabel(values: string[] | null | undefined): string {
  const roles = normalizeAnnouncementAudience(values)
  if (roles.includes('user') && roles.includes('admin')) return 'Users and admins'
  if (roles.includes('admin')) return 'Admins only'
  return 'Users only'
}

function AudienceCheckboxes({
  value,
  onChange,
  disabled = false,
}: {
  value: AnnouncementAudienceRole[]
  onChange: (roles: AnnouncementAudienceRole[]) => void
  disabled?: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {([
        ['user', 'Users'],
        ['admin', 'Admins'],
      ] as const).map(([role, label]) => (
        <button
          key={role}
          type="button"
          role="checkbox"
          aria-checked={value.includes(role)}
          disabled={disabled}
          className="inline-flex h-7 items-center gap-1.5 rounded-md px-1.5 text-sm text-foreground outline-none transition hover:bg-muted/60 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50"
          onClick={() => onChange(toggleAnnouncementAudience(value, role, !value.includes(role)))}
        >
          <span
            className={`inline-flex size-4 shrink-0 items-center justify-center rounded border transition ${
              value.includes(role)
                ? 'border-foreground bg-foreground text-background'
                : 'border-muted-foreground/50 bg-background text-transparent'
            }`}
          >
            <Check className="size-3" />
          </span>
          <span>{label}</span>
        </button>
      ))}
    </div>
  )
}

function WhatsNewAdminTab() {
  const [candidates, setCandidates] = useState<ChangeCandidate[]>([])
  const [announcements, setAnnouncements] = useState<ProductAnnouncement[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<Record<string, AnnouncementStats>>({})
  const [draftEdits, setDraftEdits] = useState<Record<string, AnnouncementEditState>>({})
  const [expandedAnnouncements, setExpandedAnnouncements] = useState<Set<string>>(() => new Set())
  const [editingAnnouncementId, setEditingAnnouncementId] = useState<string | null>(null)
  const [announcementEdits, setAnnouncementEdits] = useState<Record<string, AnnouncementEditState>>({})
  const [newTitle, setNewTitle] = useState('')
  const [newSummary, setNewSummary] = useState('')
  const [newType, setNewType] = useState('ui')
  const [newImpact, setNewImpact] = useState('medium')
  const [newAudienceRoles, setNewAudienceRoles] = useState<AnnouncementAudienceRole[]>([...DEFAULT_ANNOUNCEMENT_AUDIENCE])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [candidateData, announcementData] = await Promise.all([
        apiFetch<ChangeCandidate[]>('/api/admin/change-candidates'),
        apiFetch<ProductAnnouncement[]>('/api/admin/announcements'),
      ])
      setCandidates(candidateData)
      setAnnouncements(announcementData)
      setDraftEdits(prev => {
        const next = { ...prev }
        for (const item of candidateData) {
          if (!next[item.id]) {
            next[item.id] = {
              title: item.title,
              body: item.summary,
              severity: item.severity || 'info',
              audience_roles: normalizeAnnouncementAudience(item.audience_roles),
            }
          }
        }
        return next
      })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  async function createCandidate() {
    if (!newTitle.trim() || !newSummary.trim()) return
    setBusy('new')
    setError(null)
    try {
      await apiFetch('/api/admin/change-candidates', {
        method: 'POST',
        body: JSON.stringify({
          title: newTitle.trim(),
          summary: newSummary.trim(),
          change_type: newType,
          impact: newImpact,
          source: 'manual',
          related_files: [],
          audience_roles: newAudienceRoles,
          surfaces: ['web'],
          severity: 'info',
        }),
      })
      setNewTitle('')
      setNewSummary('')
      setNewAudienceRoles([...DEFAULT_ANNOUNCEMENT_AUDIENCE])
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function publishCandidate(candidate: ChangeCandidate) {
    const edit = draftEdits[candidate.id] ?? {
      title: candidate.title,
      body: candidate.summary,
      severity: candidate.severity || 'info',
      audience_roles: normalizeAnnouncementAudience(candidate.audience_roles),
    }
    setBusy(candidate.id)
    setError(null)
    try {
      await apiFetch(`/api/admin/change-candidates/${candidate.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({
          title: edit.title,
          body_markdown: edit.body,
          severity: edit.severity,
          audience_roles: edit.audience_roles,
          surfaces: ['web'],
        }),
      })
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function ignoreCandidate(candidateId: string) {
    setBusy(candidateId)
    setError(null)
    try {
      await apiFetch(`/api/admin/change-candidates/${candidateId}/ignore`, { method: 'POST' })
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function loadStats(announcementId: string) {
    setBusy(`stats:${announcementId}`)
    setError(null)
    try {
      const result = await apiFetch<AnnouncementStats>(`/api/admin/announcements/${announcementId}/stats`)
      setStats(prev => ({ ...prev, [announcementId]: result }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function unpublishAnnouncement(announcementId: string) {
    if (!window.confirm('Unpublish this announcement? Engineers will no longer see it, but stats will remain available.')) return
    setBusy(`unpublish:${announcementId}`)
    setError(null)
    try {
      await apiFetch(`/api/admin/announcements/${announcementId}/unpublish`, { method: 'POST' })
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function republishAnnouncement(announcementId: string) {
    if (!window.confirm('Publish this announcement again for its selected audience?')) return
    setBusy(`republish:${announcementId}`)
    setError(null)
    try {
      await apiFetch(`/api/admin/announcements/${announcementId}/republish`, { method: 'POST' })
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function deleteAnnouncement(announcementId: string) {
    if (!window.confirm('Permanently delete this announcement and its read stats from the list? This cannot be undone.')) return
    setBusy(`delete:${announcementId}`)
    setError(null)
    try {
      await apiFetch(`/api/admin/announcements/${announcementId}`, { method: 'DELETE' })
      setStats(prev => {
        const next = { ...prev }
        delete next[announcementId]
        return next
      })
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  function toggleAnnouncementExpanded(announcementId: string) {
    setExpandedAnnouncements(prev => {
      const next = new Set(prev)
      if (next.has(announcementId)) {
        next.delete(announcementId)
      } else {
        next.add(announcementId)
      }
      return next
    })
  }

  function startAnnouncementEdit(item: ProductAnnouncement) {
    setEditingAnnouncementId(item.id)
    setAnnouncementEdits(prev => ({
      ...prev,
      [item.id]: {
        title: item.title,
        body: item.body_markdown,
        severity: item.severity,
        audience_roles: normalizeAnnouncementAudience(item.audience_roles),
      },
    }))
  }

  function cancelAnnouncementEdit() {
    setEditingAnnouncementId(null)
  }

  async function saveAnnouncementEdit(item: ProductAnnouncement) {
    const edit = announcementEdits[item.id] ?? {
      title: item.title,
      body: item.body_markdown,
      severity: item.severity,
      audience_roles: normalizeAnnouncementAudience(item.audience_roles),
    }
    setBusy(`edit:${item.id}`)
    setError(null)
    try {
      await apiFetch(`/api/admin/announcements/${item.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          title: edit.title,
          body_markdown: edit.body,
          severity: edit.severity,
          audience_roles: edit.audience_roles,
          surfaces: item.surfaces?.length ? item.surfaces : ['web'],
        }),
      })
      setEditingAnnouncementId(null)
      void load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const draftCandidates = candidates.filter(item => item.status === 'draft')

  return (
    <div className="max-w-5xl space-y-4">
      <div className="rounded-lg border bg-card p-4 shadow-sm">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h2 className="text-base font-semibold">What’s New publishing queue</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Draft changes are private. Published announcements appear only for the selected audience.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={load} disabled={loading} className="gap-1.5">
            <RefreshCw className={`size-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="rounded-lg border bg-card shadow-sm">
        <div className="border-b px-4 py-3">
          <h3 className="text-sm font-semibold">Add draft change</h3>
        </div>
        <div className="grid gap-3 p-4 lg:grid-cols-[1fr_10rem_10rem_13rem_auto] lg:items-end">
          <label className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Title</span>
            <Input value={newTitle} onChange={e => setNewTitle(e.target.value)} placeholder="Short user-facing change title" />
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Type</span>
            <select className="h-9 w-full rounded-md border bg-background px-2 text-sm" value={newType} onChange={e => setNewType(e.target.value)}>
              <option value="ui">UI</option>
              <option value="feature">Feature</option>
              <option value="workflow">Workflow</option>
              <option value="admin">Admin</option>
              <option value="zendesk_app">Zendesk app</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Impact</span>
            <select className="h-9 w-full rounded-md border bg-background px-2 text-sm" value={newImpact} onChange={e => setNewImpact(e.target.value)}>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Audience</span>
            <div className="flex h-9 items-center">
              <AudienceCheckboxes value={newAudienceRoles} onChange={setNewAudienceRoles} />
            </div>
          </label>
          <Button onClick={createCandidate} disabled={busy === 'new' || !newTitle.trim() || !newSummary.trim()} className="gap-1.5">
            {busy === 'new' ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
            Add draft
          </Button>
          <label className="space-y-1 lg:col-span-5">
            <span className="text-xs font-medium text-muted-foreground">Summary</span>
            <textarea
              value={newSummary}
              onChange={e => setNewSummary(e.target.value)}
              className="min-h-20 w-full rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
              placeholder="What changed and why engineers may notice it."
            />
          </label>
        </div>
      </div>

      <div className="rounded-lg border bg-card shadow-sm">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h3 className="text-sm font-semibold">Draft changes</h3>
          <span className="text-xs text-muted-foreground">{draftCandidates.length} pending</span>
        </div>
        {loading && (
          <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading…
          </div>
        )}
        {!loading && draftCandidates.length === 0 && (
          <div className="px-4 py-3 text-sm text-muted-foreground">No draft changes waiting for review.</div>
        )}
        <div className="divide-y">
          {draftCandidates.map(candidate => {
            const edit = draftEdits[candidate.id] ?? {
              title: candidate.title,
              body: candidate.summary,
              severity: candidate.severity || 'info',
              audience_roles: normalizeAnnouncementAudience(candidate.audience_roles),
            }
            return (
              <div key={candidate.id} className="space-y-3 px-4 py-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{candidate.title}</span>
                  <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{candidate.change_type}</span>
                  <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{candidate.impact}</span>
                  <span className="ml-auto text-xs text-muted-foreground">{formatAdminDate(candidate.created_at)}</span>
                </div>
                <div className="grid gap-3 lg:grid-cols-[1fr_11rem_13rem]">
                  <label className="space-y-1">
                    <span className="text-xs font-medium text-muted-foreground">Announcement title</span>
                    <Input
                      value={edit.title}
                      onChange={e => setDraftEdits(prev => ({
                        ...prev,
                        [candidate.id]: { ...edit, title: e.target.value },
                      }))}
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="text-xs font-medium text-muted-foreground">Severity</span>
                    <select
                      className="h-9 w-full rounded-md border bg-background px-2 text-sm"
                      value={edit.severity}
                      onChange={e => setDraftEdits(prev => ({
                        ...prev,
                        [candidate.id]: { ...edit, severity: e.target.value },
                      }))}
                    >
                      <option value="info">Info</option>
                      <option value="important">Important</option>
                      <option value="breaking">Breaking</option>
                    </select>
                  </label>
                  <label className="space-y-1">
                    <span className="text-xs font-medium text-muted-foreground">Audience</span>
                    <div className="flex h-9 items-center">
                      <AudienceCheckboxes
                        value={edit.audience_roles}
                        onChange={roles => setDraftEdits(prev => ({
                          ...prev,
                          [candidate.id]: { ...edit, audience_roles: roles },
                        }))}
                      />
                    </div>
                  </label>
                  <label className="space-y-1 lg:col-span-3">
                    <span className="text-xs font-medium text-muted-foreground">Announcement body</span>
                    <textarea
                      value={edit.body}
                      onChange={e => setDraftEdits(prev => ({
                        ...prev,
                        [candidate.id]: { ...edit, body: e.target.value },
                      }))}
                      className="min-h-24 w-full rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                    />
                  </label>
                </div>
                {candidate.related_files?.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    Files: {candidate.related_files.join(', ')}
                  </p>
                )}
                <div className="flex gap-2">
                  <Button size="sm" onClick={() => void publishCandidate(candidate)} disabled={busy === candidate.id} className="gap-1.5">
                    {busy === candidate.id ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5" />}
                    Publish
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => void ignoreCandidate(candidate.id)} disabled={busy === candidate.id}>
                    Ignore
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <div className="rounded-lg border bg-card shadow-sm">
        <div className="border-b px-4 py-3">
          <h3 className="text-sm font-semibold">Announcements</h3>
        </div>
        <div className="divide-y">
          {announcements.length === 0 && (
            <div className="px-4 py-3 text-sm text-muted-foreground">No announcements published yet.</div>
          )}
          {announcements.map(item => {
            const status = item.status ?? 'published'
            const isExpanded = expandedAnnouncements.has(item.id)
            const isEditing = editingAnnouncementId === item.id
            const edit = announcementEdits[item.id] ?? {
              title: item.title,
              body: item.body_markdown,
              severity: item.severity,
              audience_roles: normalizeAnnouncementAudience(item.audience_roles),
            }
            return (
              <div key={item.id} className="px-4 py-3">
                {isEditing ? (
                  <div className="grid gap-3 lg:grid-cols-[1fr_11rem_13rem]">
                    <label className="space-y-1">
                      <span className="text-xs font-medium text-muted-foreground">Announcement title</span>
                      <Input
                        value={edit.title}
                        onChange={e => setAnnouncementEdits(prev => ({
                          ...prev,
                          [item.id]: { ...edit, title: e.target.value },
                        }))}
                      />
                    </label>
                    <label className="space-y-1">
                      <span className="text-xs font-medium text-muted-foreground">Severity</span>
                      <select
                        className="h-9 w-full rounded-md border bg-background px-2 text-sm"
                        value={edit.severity}
                        onChange={e => setAnnouncementEdits(prev => ({
                          ...prev,
                          [item.id]: { ...edit, severity: e.target.value },
                        }))}
                      >
                        <option value="info">Info</option>
                        <option value="important">Important</option>
                        <option value="breaking">Breaking</option>
                      </select>
                    </label>
                    <label className="space-y-1">
                      <span className="text-xs font-medium text-muted-foreground">Audience</span>
                      <div className="flex h-9 items-center">
                        <AudienceCheckboxes
                          value={edit.audience_roles}
                          onChange={roles => setAnnouncementEdits(prev => ({
                            ...prev,
                            [item.id]: { ...edit, audience_roles: roles },
                          }))}
                        />
                      </div>
                    </label>
                    <label className="space-y-1 lg:col-span-3">
                      <span className="text-xs font-medium text-muted-foreground">Announcement body</span>
                      <textarea
                        value={edit.body}
                        onChange={e => setAnnouncementEdits(prev => ({
                          ...prev,
                          [item.id]: { ...edit, body: e.target.value },
                        }))}
                        className="min-h-28 w-full rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                      />
                    </label>
                  </div>
                ) : (
                  <>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{item.title}</span>
                      <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{item.severity}</span>
                      {status === 'unpublished' && (
                        <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">Unpublished</span>
                      )}
                      <span className="ml-auto text-xs text-muted-foreground">{formatAdminDate(item.published_at)}</span>
                    </div>
                    <p className={`mt-1 whitespace-pre-wrap text-sm text-muted-foreground ${isExpanded ? '' : 'line-clamp-2'}`}>
                      {item.body_markdown}
                    </p>
                  </>
                )}
                <p className="mt-1 text-xs text-muted-foreground">
                  Audience: {announcementAudienceLabel(item.audience_roles)} · Surfaces: {item.surfaces.join(', ')}
                  {status === 'unpublished' && item.unpublished_at ? ` · Unpublished: ${formatAdminDate(item.unpublished_at)}` : ''}
                  {status === 'published' && item.republished_at ? ` · Republished: ${formatAdminDate(item.republished_at)}` : ''}
                  {item.edited_at ? ` · Edited: ${formatAdminDate(item.edited_at)}` : ''}
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {!isEditing && (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-7 gap-1.5 px-2"
                      onClick={() => toggleAnnouncementExpanded(item.id)}
                    >
                      {isExpanded ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
                      {isExpanded ? 'Show less' : 'Show more'}
                    </Button>
                  )}
                  {isEditing ? (
                    <>
                      <Button
                        size="sm"
                        className="h-7 gap-1.5"
                        disabled={busy === `edit:${item.id}` || !edit.title.trim() || !edit.body.trim()}
                        onClick={() => void saveAnnouncementEdit(item)}
                      >
                        {busy === `edit:${item.id}` ? <Loader2 className="size-3 animate-spin" /> : <Save className="size-3" />}
                        Save
                      </Button>
                      <Button size="sm" variant="outline" className="h-7" onClick={cancelAnnouncementEdit} disabled={busy === `edit:${item.id}`}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 gap-1.5"
                      onClick={() => startAnnouncementEdit(item)}
                    >
                      <Pencil className="size-3" />
                      Edit
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7"
                    disabled={busy === `stats:${item.id}` || isEditing}
                    onClick={() => void loadStats(item.id)}
                  >
                    {busy === `stats:${item.id}` && <Loader2 className="size-3 animate-spin" />}
                    Read stats
                  </Button>
                  {status === 'published' && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 gap-1.5"
                      disabled={busy === `unpublish:${item.id}` || isEditing}
                      onClick={() => void unpublishAnnouncement(item.id)}
                    >
                      {busy === `unpublish:${item.id}`
                        ? <Loader2 className="size-3 animate-spin" />
                        : <EyeOff className="size-3" />}
                      Unpublish
                    </Button>
                  )}
                  {status === 'unpublished' && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 gap-1.5"
                      disabled={busy === `republish:${item.id}` || isEditing}
                      onClick={() => void republishAnnouncement(item.id)}
                    >
                      {busy === `republish:${item.id}`
                        ? <Loader2 className="size-3 animate-spin" />
                        : <Eye className="size-3" />}
                      Publish
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="destructive"
                    className="h-7 gap-1.5"
                    disabled={busy === `delete:${item.id}` || isEditing}
                    onClick={() => void deleteAnnouncement(item.id)}
                  >
                    {busy === `delete:${item.id}`
                      ? <Loader2 className="size-3 animate-spin" />
                      : <Trash2 className="size-3" />}
                    Delete
                  </Button>
                  {stats[item.id] && (
                    <span className="text-xs text-muted-foreground">
                      {stats[item.id].dismissed_count} dismissed · {stats[item.id].read_count} tracked
                    </span>
                  )}
                </div>
                {stats[item.id]?.reads?.length > 0 && (
                  <div className="mt-2 max-h-28 overflow-y-auto rounded border bg-muted/30 px-2 py-1.5">
                    {stats[item.id].reads.slice(0, 20).map(read => (
                      <div key={read.id} className="flex justify-between gap-3 text-xs">
                        <span className="truncate">{read.user_email}</span>
                        <span className="shrink-0 text-muted-foreground">{formatAdminDate(read.dismissed_at)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Users tab
// ---------------------------------------------------------------------------

const PROTECTED_ADMINS = new Set(['morgan.chen@northstar.example.com', 'priya.shah@northstar.example.com'])

type Role = 'user' | 'admin' | 'super_admin'

interface UserEntry {
  email: string
  display_name?: string
  active: boolean
  role: Role
  universal_chat_logging_disabled?: boolean
}

interface ConfirmState {
  title: string
  description: string
  onConfirm: () => void
}

const ROLE_LABELS: Record<Role, string> = {
  user: 'User',
  admin: 'Admin',
  super_admin: 'Super Admin',
}

const ROLE_BADGE: Record<Role, string> = {
  user: 'bg-muted text-muted-foreground',
  admin: 'bg-primary/10 text-primary',
  super_admin: 'bg-amber-100 text-amber-800',
}

function userFallbackName(email: string): string {
  return email
}

function userDisplayName(user: UserEntry): string {
  return (user.display_name || '').trim()
}

function userPrimaryLabel(user: UserEntry): string {
  return userDisplayName(user) || userFallbackName(user.email)
}

function normalizeUserSearch(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .trim()
}

function UsersTab() {
  const { role: callerRole } = useAuth()
  const isSuperAdmin = callerRole === 'super_admin'

  const [users, setUsers] = useState<UserEntry[]>([])
  const [userSearch, setUserSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [newEmail, setNewEmail] = useState('')
  const [newRole, setNewRole] = useState<Role>('user')
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<ConfirmState | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch<{ users: UserEntry[] }>('/api/admin/users')
      setUsers(r.users)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  async function addUser() {
    const email = newEmail.trim().toLowerCase()
    if (!email || !email.includes('@')) return
    setAdding(true)
    try {
      const u = await apiFetch<UserEntry>('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({ email, role: newRole }),
      })
      setUsers(prev => [...prev.filter(x => x.email !== u.email), u].sort((a, b) => a.email.localeCompare(b.email)))
      setNewEmail('')
      setNewRole('user')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setAdding(false)
    }
  }

  async function patchUser(email: string, patch: Partial<Pick<UserEntry, 'active' | 'role' | 'universal_chat_logging_disabled'>>) {
    setBusy(email)
    try {
      const u = await apiFetch<UserEntry>(`/api/admin/users/${encodeURIComponent(email)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      })
      setUsers(prev => prev.map(x => x.email === email ? u : x))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  function confirmRoleChange(u: UserEntry, newR: Role) {
    setConfirm({
      title: `Change role to ${ROLE_LABELS[newR]}?`,
      description: `${u.email} will ${newR === 'user' ? 'lose' : 'gain'} ${ROLE_LABELS[newR] === 'User' ? 'elevated' : ROLE_LABELS[newR]} access immediately.`,
      onConfirm: () => { setConfirm(null); void patchUser(u.email, { role: newR }) },
    })
  }

  function confirmDelete(email: string) {
    setConfirm({
      title: 'Remove user?',
      description: `${email} will immediately lose access to ESIP.`,
      onConfirm: async () => {
        setConfirm(null)
        setBusy(email)
        try {
          await apiFetch(`/api/admin/users/${encodeURIComponent(email)}`, { method: 'DELETE' })
          setUsers(prev => prev.filter(x => x.email !== email))
        } catch (e) {
          setError((e as Error).message)
          setBusy(null)
        }
      },
    })
  }

  // What roles can the current caller set for a given target?
  function allowedRoles(targetRole: Role): Role[] {
    if (isSuperAdmin) return ['user', 'admin', 'super_admin']
    // admin can promote users to admin, but cannot grant super_admin or change admins.
    return targetRole === 'user' ? ['user', 'admin'] : []
  }

  const filteredUsers = useMemo(() => {
    const q = normalizeUserSearch(userSearch)
    if (!q) return users
    return users.filter((u) => {
      return normalizeUserSearch(`${userDisplayName(u)} ${u.email}`).includes(q)
    })
  }, [users, userSearch])

  return (
    <div className="max-w-4xl space-y-4">
      <div className="rounded-lg border bg-card shadow-sm">
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <div>
            <h2 className="font-medium">Access allowlist</h2>
            <p className="text-sm text-muted-foreground mt-0.5">
              Users who can log in to ESIP. Admins can access this panel.
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={load} disabled={loading} className="h-8 w-8 p-0">
            <RefreshCw className={`size-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>

        <div className="flex flex-col gap-2 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="relative w-full sm:max-w-md">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={userSearch}
              onChange={(e) => setUserSearch(e.target.value)}
              placeholder="Search name or email"
              className="h-9 pl-8 pr-8 text-sm"
              disabled={loading && users.length === 0}
            />
            {userSearch && (
              <Button
                variant="ghost"
                size="sm"
                className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 p-0 text-muted-foreground"
                onClick={() => setUserSearch('')}
                title="Clear search"
              >
                <X className="size-3.5" />
              </Button>
            )}
          </div>
          <div className="shrink-0 text-xs text-muted-foreground">
            {filteredUsers.length.toLocaleString()} of {users.length.toLocaleString()} users
          </div>
        </div>

        {error && (
          <div className="px-4 py-2 text-xs text-destructive flex items-center gap-1.5 border-b">
            <AlertTriangle className="size-3" /> {error}
          </div>
        )}

        <div className="divide-y">
          {loading && (
            <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Loading…
            </div>
          )}
          {!loading && users.length === 0 && (
            <div className="px-4 py-3 text-sm text-muted-foreground">No users found.</div>
          )}
          {!loading && users.length > 0 && filteredUsers.length === 0 && (
            <div className="px-4 py-3 text-sm text-muted-foreground">No matching users.</div>
          )}
          {filteredUsers.map((u) => {
            const isProtected = PROTECTED_ADMINS.has(u.email)
            // admin can only touch role=user; super_admin can touch non-protected
            const canEdit = !isProtected && (isSuperAdmin || u.role === 'user')
            const roleOptions = allowedRoles(u.role)
            const displayName = userDisplayName(u)
            const primaryLabel = userPrimaryLabel(u)
            return (
              <div key={u.email} className="flex items-center justify-between px-4 py-2.5 gap-3">
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  {isProtected && <span title="Protected account"><Lock className="size-3 shrink-0 text-muted-foreground" /></span>}
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate text-sm font-medium">{primaryLabel}</span>
                      <span className={`shrink-0 text-xs font-medium px-1.5 py-0.5 rounded ${ROLE_BADGE[u.role]}`}>
                        {ROLE_LABELS[u.role]}
                      </span>
                      {!u.active && (
                        <span className="shrink-0 text-xs font-medium px-1.5 py-0.5 rounded bg-red-100 text-red-700">inactive</span>
                      )}
                      {isSuperAdmin && u.universal_chat_logging_disabled && (
                        <span className="shrink-0 text-xs font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">chat log off</span>
                      )}
                    </div>
                    {displayName && <div className="truncate text-xs text-muted-foreground">{u.email}</div>}
                  </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  {/* Active toggle */}
                  {canEdit && (
                    <Button
                      variant="ghost" size="sm"
                      className={`h-7 px-2 text-xs ${u.active ? 'text-green-700' : 'text-muted-foreground'}`}
                      disabled={busy === u.email}
                      onClick={() => patchUser(u.email, { active: !u.active })}
                    >
                      {busy === u.email ? <Loader2 className="size-3 animate-spin" /> : (u.active ? 'Active' : 'Inactive')}
                    </Button>
                  )}
                  {/* Role selector */}
                  {!isProtected && roleOptions.length > 0 && (
                    <select
                      className="h-7 px-1.5 text-xs rounded border bg-background text-foreground disabled:opacity-50"
                      value={u.role}
                      disabled={busy === u.email}
                      onChange={(e) => confirmRoleChange(u, e.target.value as Role)}
                    >
                      {roleOptions.map(r => (
                        <option key={r} value={r}>{ROLE_LABELS[r]}</option>
                      ))}
                    </select>
                  )}
                  {/* Durable Universal Chat logging — super_admin only */}
                  {isSuperAdmin && (
                    <Button
                      variant="ghost" size="sm"
                      className={`h-7 px-2 text-xs ${u.universal_chat_logging_disabled ? 'text-amber-700' : 'text-muted-foreground'}`}
                      disabled={busy === u.email}
                      title="Controls durable Universal Chat logs only. Improve answer traces stay enabled."
                      onClick={() => patchUser(u.email, {
                        universal_chat_logging_disabled: !u.universal_chat_logging_disabled,
                      })}
                    >
                      {busy === u.email ? (
                        <Loader2 className="size-3 animate-spin" />
                      ) : (
                        u.universal_chat_logging_disabled ? 'Log off' : 'Log on'
                      )}
                    </Button>
                  )}
                  {/* Delete */}
                  {canEdit && (
                    <Button
                      variant="ghost" size="sm"
                      className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                      disabled={busy === u.email}
                      title="Remove user"
                      onClick={() => confirmDelete(u.email)}
                    >
                      <X className="size-3" />
                    </Button>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {/* Add user form */}
        <div className="px-4 py-3 border-t space-y-2">
          <div className="flex gap-2">
            <Input
              placeholder="alex.morgan@northstar.example.com"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void addUser() }}
              className="text-sm h-8"
            />
            {isSuperAdmin && (
              <select
                className="h-8 px-2 text-sm rounded border bg-background text-foreground"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value as Role)}
              >
                <option value="user">User</option>
                <option value="admin">Admin</option>
                <option value="super_admin">Super Admin</option>
              </select>
            )}
            <Button size="sm" className="h-8 gap-1 shrink-0" onClick={addUser} disabled={adding}>
              {adding ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
              Add
            </Button>
          </div>
        </div>
      </div>

      {/* Confirmation dialog */}
      <Dialog open={!!confirm} onOpenChange={(open) => { if (!open) setConfirm(null) }}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{confirm?.title}</DialogTitle>
            <DialogDescription>{confirm?.description}</DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button variant="outline" size="sm" onClick={() => setConfirm(null)}>Cancel</Button>
            <Button variant="destructive" size="sm" onClick={() => confirm?.onConfirm()}>Confirm</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Usage Insights tab
// ---------------------------------------------------------------------------

interface UsageDashboard {
  range: {
    mode: string
    start_at: string | null
    end_at: string | null
    include_translation_turns: boolean
    user_email: string | null
    question_type: string | null
  }
  controls: {
    compare_previous: boolean
    group_by: string
    min_activity: number
    problem_signal: string
    category_focus: string
    technical_quality_enabled: boolean
  }
  summary: {
    total_queries_to_date: number
    total_queries_in_range: number
    active_users_in_range: number
    avg_queries_per_active_user: number
    weak_or_no_answer_turns: number
    no_source_turns: number
    top_question_type: string | null
    usage_by_role: Array<{ role: string; count: number }>
  }
  comparison: {
    start_at: string | null
    end_at: string | null
    total_queries_in_range: number
    active_users_in_range: number
    weak_or_no_answer_turns: number
    no_source_turns: number
  } | null
  users: Array<{
    email: string | null
    display_name: string
    role: string
    active: boolean
    first_seen_at: string | null
    last_active_at: string | null
    queries_to_date: number
    queries_in_range: number
    avg_latency_ms: number | null
    weak_or_no_answer: number
    no_source_count: number
    evidence_rate_pct: number
    documentation_opportunities: number
    active_days: number
    top_question_type: string | null
  }>
  users_unfiltered_count: number
  question_types: Array<{
    question_type: string
    count: number
    users: number
    weak_or_no_answer: number
    no_source_count: number
    evidence_rate_pct: number
  }>
  timeseries: Array<{
    date: string
    queries: number
    active_users: number
    weak_or_no_answer: number
    no_source: number
  }>
  grouped_breakdown: Array<{
    key: string
    label: string
    count: number
    users: number
    share_pct: number
    active_days: number
    last_active_at: string | null
    top_question_type: string | null
    weak_or_no_answer: number
    no_source_count: number
    evidence_rate_pct: number
    documentation_opportunities: number
  }>
  source_coverage: {
    total_turns: number
    with_sources: number
    with_tickets: number
    with_analytics: number
    with_evidence: number
    without_sources: number
    source_coverage_pct: number
    evidence_coverage_pct: number
    ticket_usage_pct: number
  }
  document_gaps: Array<{
    theme: string
    question_type: string
    count: number
    users_affected: number
    no_source_count: number
    weak_or_no_answer: number
    example_question: string
    suggested_action: string
  }>
  filters: {
    users: Array<{ email: string; display_name: string; role: string; queries_to_date: number }>
    question_types: string[]
  }
  notes: string[]
}

interface QualityTelemetryBucket {
  name: string
  count: number
}

interface QualityTelemetryRecentEvent {
  created_at?: string | null
  mode?: string | null
  quality?: string | null
  risk_classes?: string[]
  validator_action?: string | null
  answer_policy?: string | null
  repair_action?: string | null
  repair_stop_reason?: string | null
  model?: string | null
  model_surface?: string | null
  latency_ms?: number | null
  estimated_cost_cad?: number | null
  trace_id?: string | null
  turn_id?: string | null
  conversation_id?: string | null
  requester_email?: string | null
}

interface QualityTelemetryReviewRow {
  created_at?: string | null
  trace_id?: string | null
  turn_id?: string | null
  conversation_id?: string | null
  target_ticket_id?: string | number | null
  event_count: number
  modes: string[]
  qualities: string[]
  conflicting_verdict: boolean
  reason_tags: string[]
  latency_ms?: number | null
  answer_chars: number
  sources_count: number
  tickets_count: number
}

interface QualityTelemetryStageSummary {
  stage: string
  count: number
  p50_ms: number | null
  p90_ms: number | null
  max_ms: number | null
}

interface QualityTelemetryDashboard {
  range: {
    timezone: string
    start_date: string
    end_date: string
    start_at: string
    end_at: string
    max_days: number
  }
  summary: {
    quality_event_count: number
    unique_answer_count: number
    auto_validator_event_count: number
    manual_review_event_count: number
    duplicate_event_count: number
    conflicting_verdict_count: number
    turn_count: number
    suspect_event_count: number
    repair_attempt_count: number
    repair_validation_count: number
    repair_revalidation_count: number
  }
  coverage: {
    quality_events_available: boolean
    turn_logs_available: boolean
    quality_events_count: number
    turn_logs_count: number
    partial: boolean
  }
  quality_distribution: QualityTelemetryBucket[]
  unique_turn_quality_distribution: QualityTelemetryBucket[]
  validator_actions: QualityTelemetryBucket[]
  risk_classes: QualityTelemetryBucket[]
  repair_actions: QualityTelemetryBucket[]
  repair_stop_reasons: QualityTelemetryBucket[]
  model_surfaces: QualityTelemetryBucket[]
  latency: {
    count: number
    avg_ms: number | null
    p95_ms: number | null
    max_ms: number | null
  }
  stage_timings: {
    available: boolean
    turn_count: number
    measured_turn_count: number
    unmeasured_turn_count: number
    partial_measured_turn_count: number
    stages: QualityTelemetryStageSummary[]
    missing_by_stage: Array<{ stage: string; count: number }>
    caveat: string
  }
  error_provenance: {
    event_count: number
    fallback_used_count: number
    sources: QualityTelemetryBucket[]
    outcomes: QualityTelemetryBucket[]
    endpoint_classes: QualityTelemetryBucket[]
    status_codes: QualityTelemetryBucket[]
    reason_codes: QualityTelemetryBucket[]
  }
  cost: {
    count: number
    estimated_total_cad: number
    estimated_avg_cad: number | null
  }
  trend: Array<Record<string, string | number>>
  recent_events: QualityTelemetryRecentEvent[]
  review_queue: {
    available_reason_tags: string[]
    selected_reason_tag?: string | null
    thresholds: {
      too_long_answer_chars: number
      slow_latency_ms: number
    }
    answer_count: number
    untagged_answer_count: number
    reason_distribution: QualityTelemetryBucket[]
    rows: QualityTelemetryReviewRow[]
  }
  warnings: string[]
  caveats: string[]
  filters?: {
    event_type?: string | null
    quality?: string | null
    risk?: string | null
    validator_action?: string | null
    repair_action?: string | null
    stop_reason?: string | null
    model_surface?: string | null
    requester_email?: string | null
    reason_tag?: string | null
    limit: number
  }
  source_counts?: {
    quality_events_before_filters: number
    quality_events_after_filters: number
    turn_logs: number
    provenance_events: number
  }
}

interface AiDispatcherConfig {
  mode: string
  enabled: boolean
  answer_affecting_allowed?: boolean
  planner_daily_cap_cad: number
  max_planner_calls_per_turn: number
  max_tool_actions: number
  max_explicit_ticket_ids: number
  max_action_rows: number
  max_output_tokens: number
  eligible_task_classes: string[]
  assistive_task_classes: string[]
  shadow_task_classes: string[]
  allowed_modes: string[]
  blocked_modes: string[]
  allowed_task_classes: string[]
  allowed_assistive_task_classes: string[]
  allowed_shadow_task_classes: string[]
  primary_eligible_task_classes?: string[]
  primary_answer_affecting_task_classes?: string[]
  updated_at?: string | null
  updated_by?: string | null
  emergency_disabled?: boolean
  emergency_disabled_at?: string | null
  emergency_disabled_by?: string | null
}

interface ModelTransitionCaptureConfig {
  enabled: boolean
  allowed_emails: string[]
  max_allowed_emails: number
  updated_at?: string | null
  updated_by?: string | null
}

interface AiDispatcherTrace {
  created_at?: string | null
  trace_id?: string | null
  turn_id?: string | null
  conversation_id?: string | null
  mode?: string | null
  eligible?: boolean
  planner_attempted?: boolean
  planner_calls?: number
  accepted?: boolean
  answer_affecting?: boolean
  assistive_task_class?: string | null
  fallback_reason?: string | null
  reason_codes?: string[]
  latency_ms?: number | null
  estimated_cost_cad?: number | null
  actual_cost_cad?: number | null
  model_surface?: string | null
  result_set_saved?: boolean
  result_set_comparison?: boolean
}

interface AiDispatcherDashboard {
  config: AiDispatcherConfig
  range: {
    timezone: string
    start_date: string
    end_date: string
    start_at: string
    end_at: string
    max_days: number
  }
  summary: {
    trace_count: number
    eligible_turns: number
    planner_attempted_turns: number
    accepted_plans: number
    vetoed_or_rejected_plans: number
    answer_affecting_turns: number
    partial: boolean
  }
  mode_distribution: QualityTelemetryBucket[]
  fallback_reasons: QualityTelemetryBucket[]
  reason_codes: QualityTelemetryBucket[]
  latency: {
    count: number
    p50_ms: number | null
    p90_ms: number | null
    max_ms: number | null
  }
  turn_latency: {
    count: number
    p50_ms: number | null
    p90_ms: number | null
    max_ms: number | null
  }
  class_exposure: Array<{
    task_class: string
    candidate_turns: number
    planner_attempted_turns: number
    accepted_plans: number
    rejected_plans: number
    suppressed_or_locked_turns: number
    answer_affecting_turns: number
    quality_turns: number
    quality_distribution: QualityTelemetryBucket[]
    reason_codes: QualityTelemetryBucket[]
    planner_latency: {
      count: number
      p50_ms: number | null
      p90_ms: number | null
      max_ms: number | null
    }
    turn_latency: {
      count: number
      p50_ms: number | null
      p90_ms: number | null
      max_ms: number | null
    }
  }>
  quality_join: {
    quality_event_count: number
    unique_quality_answer_traces: number
    duplicate_quality_events: number
    matched_dispatcher_traces: number
  }
  cost: {
    estimated_total_cad: number
    actual_total_cad: number
    estimated_count: number
    actual_count: number
  }
  recent_traces: AiDispatcherTrace[]
  warnings: string[]
  caveats: string[]
}

interface ServiceDeskKpiMetricRow {
  email: string
  display_name: string
  inbound_calls: number
  avg_call_time_seconds: number | null
  new_assigned_tickets: number
  same_day_solved_tickets: number
  solved_tickets: number
  outstanding_tickets: number
  median_resolution_hours: number | null
  single_cycle_solved: number
  single_cycle_total: number
  single_cycle_solve_rate_pct: number
  sla_breached: number
  sla_known: number
  sla_breach_rate_pct: number
  first_reply_tickets: number
  median_first_public_reply_hours: number | null
  customer_reply_count: number
  median_customer_reply_hours: number | null
}

interface ServiceDeskKpiGroupRow extends ServiceDeskKpiMetricRow {
  group_key: string
  member_names: string[]
  mapped_row_names: string[]
  active_rows: number
  unmapped: boolean
  assignment_notes: string[]
}

interface ServiceDeskKpiGroupDetailRow extends ServiceDeskKpiMetricRow {
  group_name?: string
  row_type?: string
}

interface ServiceDeskKpiGroupSection {
  group_name: string
  group_key: string
  total: ServiceDeskKpiGroupRow
  rows: ServiceDeskKpiGroupDetailRow[]
}

interface ServiceDeskKpiReport {
  date: string
  timezone: string
  generated_at: string
  summary: {
    active_engineers: number
    inbound_calls: number
    avg_call_time_seconds: number | null
    new_assigned_tickets: number
    same_day_solved_tickets: number
    solved_tickets: number
    outstanding_tickets: number
    customer_replies: number
    median_first_public_reply_hours: number | null
    median_customer_reply_hours: number | null
    single_cycle_solved: number
    single_cycle_total: number
    single_cycle_solve_rate_pct: number
    sla_breached: number
    sla_known: number
    sla_breach_rate_pct: number
  }
  coverage: {
    relevant_tickets: number
    tickets_with_internal_assignee_email: number
    assignee_email_coverage_pct: number
    public_agent_replies: number
    public_agent_replies_with_email: number
    comment_author_email_coverage_pct: number
    telephony?: {
      status?: string
      available?: boolean
      contact_count?: number
      agent_count?: number
      api_total_records?: number
      warning?: string
    }
  }
  rows: ServiceDeskKpiMetricRow[]
  group_rows?: ServiceDeskKpiGroupRow[]
  group_sections?: ServiceDeskKpiGroupSection[]
  grouping?: {
    source?: string
    multi_group_members?: Array<{
      name: string
      groups: string[]
      rule?: string
    }>
    unmapped_active_rows?: number
    warnings?: string[]
  }
  notes: string[]
  warnings: string[]
}

interface ServiceDeskKpiSnapshotFile {
  report_date: string
  bucket: string
  blob_name: string
  gcs_uri: string
  filename: string
  media_type: string
  size_bytes: number
  updated_at: string | null
}

interface ServiceDeskKpiSnapshotList {
  bucket: string
  prefix: string
  items: ServiceDeskKpiSnapshotFile[]
}

interface ServiceDeskKpiDeliverySlot {
  id: string
  time: string
  enabled: boolean
  label?: string | null
}

interface ServiceDeskKpiDeliveryConfig {
  enabled: boolean
  timezone: string
  recipients: string[]
  cc_recipients?: string[]
  slots: ServiceDeskKpiDeliverySlot[]
  last_delivery?: Record<string, unknown> | null
  last_test?: Record<string, unknown> | null
  transport: string
  safe_test_recipients: string[]
  scheduler?: {
    enabled?: boolean
    state?: string
    schedule?: string
    schedule_time?: string | null
    time_zone?: string
  } | null
}

const EMAIL_ADDRESS_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi

function parseEmailAddresses(value: string): string[] {
  const seen = new Set<string>()
  const matches = value.match(EMAIL_ADDRESS_RE) ?? []
  for (const item of matches) {
    seen.add(item.toLowerCase())
  }
  return [...seen]
}

function KpiRecipientManager({
  label,
  emails,
  filteredEmails,
  search,
  onSearchChange,
  newEmail,
  onNewEmailChange,
  bulkValue,
  onBulkChange,
  parsedBulkCount,
  bulkNewCount,
  bulkDuplicateCount,
  loading,
  saving,
  addSavingKey,
  itemSavingPrefix,
  onAddOne,
  onRemoveOne,
  onAddBulk,
}: {
  label: string
  emails: string[]
  filteredEmails: string[]
  search: string
  onSearchChange: (value: string) => void
  newEmail: string
  onNewEmailChange: (value: string) => void
  bulkValue: string
  onBulkChange: (value: string) => void
  parsedBulkCount: number
  bulkNewCount: number
  bulkDuplicateCount: number
  loading: boolean
  saving: string | null
  addSavingKey: string
  itemSavingPrefix: string
  onAddOne: () => void
  onRemoveOne: (email: string) => void
  onAddBulk: () => void
}) {
  return (
    <div className="min-w-0 space-y-2 rounded-md border bg-muted/10 p-2">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">{label}</div>
        <div className="text-xs tabular-nums text-muted-foreground">
          {filteredEmails.length === emails.length ? `${emails.length} total` : `${filteredEmails.length} of ${emails.length}`}
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-1.5">
        <div className="relative min-w-0">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder={`Search ${label}`}
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            className="h-8 min-w-0 pl-7 text-xs"
          />
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 shrink-0 px-2"
          onClick={() => onSearchChange('')}
          disabled={!search}
        >
          Clear
        </Button>
      </div>

      <div className="divide-y overflow-hidden rounded-md border bg-background">
        <div className="max-h-40 overflow-y-auto">
          {loading ? (
            <div className="flex items-center gap-2 px-2 py-2 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              Loading {label}...
            </div>
          ) : emails.length === 0 ? (
            <div className="px-2 py-2 text-xs text-muted-foreground">No {label} recipients configured.</div>
          ) : filteredEmails.length === 0 ? (
            <div className="px-2 py-2 text-xs text-muted-foreground">No {label} recipients match this search.</div>
          ) : filteredEmails.map((email, index) => (
            <div key={email} className="grid min-h-8 grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-1.5 px-2 py-1.5">
              <span className="text-[11px] tabular-nums text-muted-foreground">{index + 1}</span>
              <span className="min-w-0 truncate text-xs" title={email}>{email}</span>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 shrink-0 p-0 text-muted-foreground hover:text-destructive"
                disabled={saving === `${itemSavingPrefix}:${email}`}
                onClick={() => onRemoveOne(email)}
                title={`Remove ${label} recipient`}
              >
                {saving === `${itemSavingPrefix}:${email}` ? <Loader2 className="size-3 animate-spin" /> : <Trash2 className="size-3" />}
              </Button>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-1.5">
        <Input
          placeholder="alex.morgan@northstar.example.com"
          value={newEmail}
          onChange={(event) => onNewEmailChange(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') onAddOne() }}
          className="h-8 min-w-0 text-xs"
        />
        <Button size="sm" className="h-8 shrink-0 px-2" onClick={onAddOne} disabled={saving === addSavingKey}>
          {saving === addSavingKey ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
          Add
        </Button>
      </div>

      <details className="rounded-md border bg-background p-2">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-[11px] font-medium uppercase tracking-normal text-muted-foreground">
          <span>Bulk paste</span>
          <span className="text-[11px] font-normal normal-case tabular-nums">
            {bulkNewCount} new{bulkDuplicateCount > 0 ? ` · ${bulkDuplicateCount} duplicate` : ''}
          </span>
        </summary>
        <div className="mt-2 space-y-1.5">
          <textarea
            value={bulkValue}
            onChange={(event) => onBulkChange(event.target.value)}
            placeholder="Name <alex.morgan@northstar.example.com>, alex.morgan@northstar.example.com"
            className="min-h-20 w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-xs outline-none transition-colors placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5">
            <div className="truncate text-[11px] text-muted-foreground">
              {parsedBulkCount > 0 ? `${parsedBulkCount} valid email${parsedBulkCount === 1 ? '' : 's'} detected` : 'No addresses detected'}
            </div>
            <Button
              size="sm"
              className="h-7 shrink-0 px-2"
              onClick={onAddBulk}
              disabled={saving === 'config' || bulkNewCount === 0}
            >
              {saving === 'config' ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
              Add {bulkNewCount || ''}
            </Button>
          </div>
        </div>
      </details>
    </div>
  )
}

function fmtDateShort(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value.slice(0, 10)
  return d.toLocaleDateString('en-CA', { month: 'short', day: '2-digit', year: 'numeric' })
}

function fmtDateTimeShort(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value.slice(0, 16)
  return d.toLocaleString('en-CA', {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

function shortQuestionType(value: string | null | undefined): string {
  if (!value) return '—'
  const map: Record<string, string> = {
    'Analytics / reporting': 'Analytics',
    'Similar past cases': 'Similar cases',
    'Document lookup': 'Docs',
    'Procedure / how-to': 'Procedures',
    'Access / login / user support': 'Access',
  }
  return map[value] ?? value.replace(' / ', ' / ')
}

function personLabel(user: {
  display_name?: string | null
  email?: string | null
  key?: string | null
}): string {
  return (user.display_name || '').trim() || user.email || user.key || 'Unknown user'
}

function syntheticOwnerKey(value: string | null | undefined): boolean {
  const safe = (value || '').trim().toLowerCase()
  return safe === '__blank__' || safe.startsWith('__name__:') || safe.startsWith('__')
}

function personSecondaryLabel(user: {
  display_name?: string | null
  email?: string | null
}): string {
  const email = (user.email || '').trim()
  if (!email || syntheticOwnerKey(email)) return ''
  return (user.display_name || '').trim() ? email : ''
}

function shortPersonLabel(user: {
  display_name?: string | null
  email?: string | null
  key?: string | null
}, max = 18): string {
  const label = personLabel(user)
  return label.length > max ? `${label.slice(0, max)}...` : label
}

function fmtCountFraction(part: number | null | undefined, total: number | null | undefined, label: string): string {
  const safePart = Number(part || 0)
  const safeTotal = Number(total || 0)
  if (safeTotal <= 0) return `0 ${label}`
  return `${safePart.toLocaleString()} of ${safeTotal.toLocaleString()} ${label}`
}

function fmtSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const totalSeconds = Math.max(0, Math.round(Number(value) || 0))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return `${minutes}:${seconds.toString().padStart(2, '0')}`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return `${hours}:${remainingMinutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function fmtDelta(current: number, previous: number | null | undefined): string {
  if (previous === null || previous === undefined) return 'No comparison'
  const diff = current - previous
  if (previous === 0) {
    if (current === 0) return 'No change vs previous'
    return `+${current.toLocaleString()} vs previous`
  }
  const pct = Math.round((diff / previous) * 100)
  const sign = diff > 0 ? '+' : ''
  return `${sign}${pct}% vs previous`
}

function MetricDetail({
  primary,
  comparison,
}: { primary: ReactNode; comparison?: ReactNode }) {
  return (
    <span className="block space-y-0.5">
      <span className="block">{primary}</span>
      {comparison && <span className="block">{comparison}</span>}
    </span>
  )
}

function InfoTip({
  title,
  children,
}: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [style, setStyle] = useState<CSSProperties>({})
  const buttonRef = useRef<HTMLButtonElement | null>(null)

  const toggle = () => {
    const next = !open
    if (next && buttonRef.current) {
      const rect = buttonRef.current.getBoundingClientRect()
      const width = 288
      const left = Math.min(Math.max(12, rect.right - width), window.innerWidth - width - 12)
      const top = Math.min(rect.bottom + 8, window.innerHeight - 180)
      setStyle({ left, top, width })
    }
    setOpen(next)
  }

  return (
    <span className="relative inline-flex">
      <button
        ref={buttonRef}
        type="button"
        className="inline-flex size-6 items-center justify-center rounded-full border bg-background text-muted-foreground shadow-sm hover:bg-muted hover:text-foreground"
        aria-label={`About ${title}`}
        onClick={toggle}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
      >
        <Info className="size-3.5" />
      </button>
      {open && createPortal((
        <span
          className="fixed z-[100] rounded-md border bg-popover p-3 text-left text-xs leading-relaxed text-popover-foreground shadow-lg"
          style={style}
        >
          <span className="block font-semibold text-foreground">{title}</span>
          <span className="mt-1 block text-muted-foreground">{children}</span>
        </span>
      ), document.body)}
    </span>
  )
}

function UsageMetricCard({
  title, value, detail, icon, info, compact = false,
}: { title: string; value: ReactNode; detail?: ReactNode; icon?: ReactNode; info?: ReactNode; compact?: boolean }) {
  return (
    <div className={`rounded-lg border bg-card shadow-sm ${compact ? 'p-2.5' : 'p-4'}`}>
      <div className={`flex items-start justify-between ${compact ? 'gap-2' : 'gap-3'}`}>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className={`${compact ? 'text-[10px]' : 'text-xs'} font-medium uppercase tracking-normal text-muted-foreground`}>{title}</p>
            {info && <InfoTip title={title}>{info}</InfoTip>}
          </div>
          <div className={`${compact ? 'mt-0.5 text-xl' : 'mt-1 text-2xl'} font-semibold tabular-nums text-foreground`}>{value}</div>
          {detail && <div className={`${compact ? 'mt-0.5 text-[11px] leading-tight' : 'mt-1 text-xs'} text-muted-foreground`}>{detail}</div>}
        </div>
        {icon && <div className={`rounded-md border bg-muted/40 text-muted-foreground ${compact ? 'p-1.5' : 'p-2'}`}>{icon}</div>}
      </div>
    </div>
  )
}

function dateInputValueInBusinessTime(offsetDays = 0): string {
  const target = new Date(Date.now() + offsetDays * 24 * 60 * 60 * 1000)
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(target)
  const byType = Object.fromEntries(parts.map(part => [part.type, part.value]))
  return `${byType.year}-${byType.month}-${byType.day}`
}

function formatReportDate(value: string): string {
  const d = new Date(`${value}T12:00:00`)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })
}

function formatKpiColumnDate(value: string): string {
  const d = new Date(`${value}T12:00:00`)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString('en-US', { month: 'numeric', day: 'numeric', year: 'numeric' })
}

function addDaysToDateInput(value: string, days: number): string {
  const d = new Date(`${value}T12:00:00`)
  if (Number.isNaN(d.getTime())) return dateInputValueInBusinessTime(days)
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

function inclusiveDateRangeDays(startDate: string, endDate: string): number | null {
  const start = new Date(`${startDate}T12:00:00`)
  const end = new Date(`${endDate}T12:00:00`)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null
  const diffDays = Math.round((end.getTime() - start.getTime()) / (24 * 60 * 60 * 1000))
  return diffDays >= 0 ? diffDays + 1 : null
}

function downloadFilename(contentDisposition: string | null, fallback: string): string {
  const match = /filename="?([^"]+)"?/i.exec(contentDisposition || '')
  return match?.[1] || fallback
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`
}

function formatDateTimeShort(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

const SERVICE_DESK_KPI_MAX_RANGE_DAYS = 14

type ServiceDeskKpiTableSortField =
  | 'group'
  | 'assignee'
  | 'inbound_calls'
  | 'avg_call_time_seconds'
  | 'new_assigned_tickets'
  | 'same_day_solved_tickets'
  | 'outstanding_tickets'
  | 'single_cycle_solve_rate_pct'

type ServiceDeskKpiTableSort = {
  field: ServiceDeskKpiTableSortField
  direction: 'asc' | 'desc'
  date?: string
} | null

type ServiceDeskKpiGroupedItem = {
  key: string
  groupLabel: string
  assigneeLabel: string
  rowType: 'report_total' | 'group_total' | 'assignee' | 'queue'
  byDate: Record<string, ServiceDeskKpiMetricRow | undefined>
}

const SERVICE_DESK_KPI_METRIC_SORT_FIELDS = new Set<ServiceDeskKpiTableSortField>([
  'inbound_calls',
  'avg_call_time_seconds',
  'new_assigned_tickets',
  'same_day_solved_tickets',
  'outstanding_tickets',
  'single_cycle_solve_rate_pct',
])

const defaultServiceDeskKpiSortDirection = (field: ServiceDeskKpiTableSortField): 'asc' | 'desc' =>
  SERVICE_DESK_KPI_METRIC_SORT_FIELDS.has(field) ? 'desc' : 'asc'

function serviceDeskKpiSortNumber(
  item: ServiceDeskKpiGroupedItem,
  sort: ServiceDeskKpiTableSort,
): number | undefined {
  if (!sort?.date || !SERVICE_DESK_KPI_METRIC_SORT_FIELDS.has(sort.field)) return undefined
  const row = item.byDate[sort.date]
  if (!row) return undefined
  if (sort.field === 'avg_call_time_seconds') return row.avg_call_time_seconds ?? undefined
  if (sort.field === 'single_cycle_solve_rate_pct') {
    return row.single_cycle_total > 0 ? row.single_cycle_solve_rate_pct : undefined
  }
  switch (sort.field) {
    case 'inbound_calls':
      return row.inbound_calls
    case 'new_assigned_tickets':
      return row.new_assigned_tickets
    case 'same_day_solved_tickets':
      return row.same_day_solved_tickets
    case 'outstanding_tickets':
      return row.outstanding_tickets
    default:
      return undefined
  }
}

function compareServiceDeskKpiItems(
  left: ServiceDeskKpiGroupedItem,
  right: ServiceDeskKpiGroupedItem,
  sort: ServiceDeskKpiTableSort,
): number {
  if (!sort) return 0
  const direction = sort.direction === 'asc' ? 1 : -1
  if (sort.field === 'group') {
    const result = left.groupLabel.localeCompare(right.groupLabel, undefined, { sensitivity: 'base' })
    return result * direction
  }
  if (sort.field === 'assignee') {
    const result = left.assigneeLabel.localeCompare(right.assigneeLabel, undefined, { sensitivity: 'base' })
    return result * direction
  }
  const leftValue = serviceDeskKpiSortNumber(left, sort)
  const rightValue = serviceDeskKpiSortNumber(right, sort)
  if (leftValue === undefined && rightValue === undefined) return 0
  if (leftValue === undefined) return 1
  if (rightValue === undefined) return -1
  if (leftValue === rightValue) {
    return left.assigneeLabel.localeCompare(right.assigneeLabel, undefined, { sensitivity: 'base' })
  }
  return (leftValue - rightValue) * direction
}

function sectionNameForKey(
  groupKey: string,
  reports: ServiceDeskKpiReport[],
  sectionsByDate: Map<string, Map<string, ServiceDeskKpiGroupSection>>,
): string {
  for (const report of [...reports].reverse()) {
    const name = sectionsByDate.get(report.date)?.get(groupKey)?.group_name
    if (name) return name
  }
  return groupKey
}

function firstRowForKey(
  rowKey: string,
  reports: ServiceDeskKpiReport[],
  rowsByDate: Map<string, Map<string, ServiceDeskKpiGroupDetailRow>>,
): ServiceDeskKpiGroupDetailRow | undefined {
  for (const report of [...reports].reverse()) {
    const row = rowsByDate.get(report.date)?.get(rowKey)
    if (row) return row
  }
  return undefined
}

function ServiceDeskKpiDeliveryCard() {
  const [config, setConfig] = useState<ServiceDeskKpiDeliveryConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [newRecipient, setNewRecipient] = useState('')
  const [recipientSearch, setRecipientSearch] = useState('')
  const [bulkRecipients, setBulkRecipients] = useState('')
  const [newCcRecipient, setNewCcRecipient] = useState('')
  const [ccRecipientSearch, setCcRecipientSearch] = useState('')
  const [bulkCcRecipients, setBulkCcRecipients] = useState('')
  const [testRecipient, setTestRecipient] = useState('')
  const [slotDrafts, setSlotDrafts] = useState<Record<string, { time: string; label: string }>>({})
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  const loadDelivery = useCallback(async () => {
    setLoading(true)
    setMessage(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiDeliveryConfig>('/api/admin/usage/service-desk-kpi/delivery')
      setConfig(payload)
      setSlotDrafts(Object.fromEntries((payload.slots ?? []).map(slot => [
        slot.id,
        { time: slot.time, label: slot.label ?? '' },
      ])))
      setTestRecipient(current => current || payload.safe_test_recipients?.[0] || 'morgan.chen@northstar.example.com')
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadDelivery() }, [loadDelivery])

  const patchDelivery = useCallback(async (patch: Partial<Pick<ServiceDeskKpiDeliveryConfig, 'enabled' | 'timezone' | 'recipients' | 'cc_recipients' | 'slots'>>) => {
    setSaving('config')
    setMessage(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiDeliveryConfig>('/api/admin/usage/service-desk-kpi/delivery', {
        method: 'PATCH',
        body: JSON.stringify(patch),
      })
      setConfig(payload)
      setSlotDrafts(Object.fromEntries((payload.slots ?? []).map(slot => [
        slot.id,
        { time: slot.time, label: slot.label ?? '' },
      ])))
      setMessage({ type: 'success', text: 'Delivery settings saved' })
      return payload
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message })
      return null
    } finally {
      setSaving(null)
    }
  }, [])

  const configuredRecipients = useMemo(() => config?.recipients ?? [], [config?.recipients])
  const configuredCcRecipients = useMemo(() => config?.cc_recipients ?? [], [config?.cc_recipients])
  const filteredRecipients = useMemo(() => {
    const query = recipientSearch.trim().toLowerCase()
    if (!query) return configuredRecipients
    return configuredRecipients.filter(email => email.includes(query))
  }, [configuredRecipients, recipientSearch])
  const filteredCcRecipients = useMemo(() => {
    const query = ccRecipientSearch.trim().toLowerCase()
    if (!query) return configuredCcRecipients
    return configuredCcRecipients.filter(email => email.includes(query))
  }, [configuredCcRecipients, ccRecipientSearch])
  const parsedBulkRecipients = useMemo(() => parseEmailAddresses(bulkRecipients), [bulkRecipients])
  const parsedBulkCcRecipients = useMemo(() => parseEmailAddresses(bulkCcRecipients), [bulkCcRecipients])
  const bulkRecipientStats = useMemo(() => {
    const existing = new Set([...configuredRecipients, ...configuredCcRecipients])
    const next = parsedBulkRecipients.filter(email => !existing.has(email))
    return {
      next,
      duplicateCount: parsedBulkRecipients.length - next.length,
    }
  }, [configuredCcRecipients, configuredRecipients, parsedBulkRecipients])
  const bulkCcRecipientStats = useMemo(() => {
    const existing = new Set([...configuredRecipients, ...configuredCcRecipients])
    const next = parsedBulkCcRecipients.filter(email => !existing.has(email))
    return {
      next,
      duplicateCount: parsedBulkCcRecipients.length - next.length,
    }
  }, [configuredCcRecipients, configuredRecipients, parsedBulkCcRecipients])

  const addRecipient = useCallback(async () => {
    const email = newRecipient.trim().toLowerCase()
    if (!email || !email.includes('@')) return
    setSaving('recipient:add')
    setMessage(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiDeliveryConfig>('/api/admin/usage/service-desk-kpi/delivery/recipients', {
        method: 'POST',
        body: JSON.stringify({ email }),
      })
      setConfig(payload)
      setNewRecipient('')
      setMessage({ type: 'success', text: 'Recipient added' })
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message })
    } finally {
      setSaving(null)
    }
  }, [newRecipient])

  const removeRecipient = useCallback(async (email: string) => {
    setSaving(`recipient:${email}`)
    setMessage(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiDeliveryConfig>(
        `/api/admin/usage/service-desk-kpi/delivery/recipients/${encodeURIComponent(email)}`,
        { method: 'DELETE' },
      )
      setConfig(payload)
      setMessage({ type: 'success', text: 'Recipient removed' })
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message })
    } finally {
      setSaving(null)
    }
  }, [])

  const addBulkRecipients = useCallback(async () => {
    if (!config || bulkRecipientStats.next.length === 0) return
    const payload = await patchDelivery({ recipients: [...config.recipients, ...bulkRecipientStats.next] })
    if (payload) {
      setBulkRecipients('')
      setRecipientSearch('')
      setMessage({ type: 'success', text: `Added ${bulkRecipientStats.next.length} KPI recipient${bulkRecipientStats.next.length === 1 ? '' : 's'}` })
    }
  }, [bulkRecipientStats.next, config, patchDelivery])

  const addCcRecipient = useCallback(async () => {
    const email = newCcRecipient.trim().toLowerCase()
    if (!config || !email || !email.includes('@')) return
    setSaving('cc_recipient:add')
    setMessage(null)
    const payload = await patchDelivery({ cc_recipients: [...configuredCcRecipients, email] })
    if (payload) {
      setNewCcRecipient('')
      setMessage({ type: 'success', text: 'CC recipient added' })
    }
  }, [configuredCcRecipients, config, newCcRecipient, patchDelivery])

  const removeCcRecipient = useCallback(async (email: string) => {
    if (!config) return
    setSaving(`cc_recipient:${email}`)
    setMessage(null)
    const payload = await patchDelivery({ cc_recipients: configuredCcRecipients.filter(item => item !== email) })
    if (payload) {
      setMessage({ type: 'success', text: 'CC recipient removed' })
    }
  }, [configuredCcRecipients, config, patchDelivery])

  const addBulkCcRecipients = useCallback(async () => {
    if (!config || bulkCcRecipientStats.next.length === 0) return
    const payload = await patchDelivery({ cc_recipients: [...configuredCcRecipients, ...bulkCcRecipientStats.next] })
    if (payload) {
      setBulkCcRecipients('')
      setCcRecipientSearch('')
      setMessage({ type: 'success', text: `Added ${bulkCcRecipientStats.next.length} CC recipient${bulkCcRecipientStats.next.length === 1 ? '' : 's'}` })
    }
  }, [bulkCcRecipientStats.next, configuredCcRecipients, config, patchDelivery])

  const saveSlot = useCallback(async (slot: ServiceDeskKpiDeliverySlot, patch?: Partial<ServiceDeskKpiDeliverySlot>) => {
    const draft = slotDrafts[slot.id] ?? { time: slot.time, label: slot.label ?? '' }
    const next = {
      time: patch?.time ?? draft.time,
      label: patch?.label ?? draft.label,
      enabled: patch?.enabled ?? slot.enabled,
    }
    setSaving(`slot:${slot.id}`)
    setMessage(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiDeliveryConfig>(
        `/api/admin/usage/service-desk-kpi/delivery/slots/${encodeURIComponent(slot.id)}`,
        {
          method: 'PUT',
          body: JSON.stringify(next),
        },
      )
      setConfig(payload)
      setSlotDrafts(Object.fromEntries((payload.slots ?? []).map(item => [
        item.id,
        { time: item.time, label: item.label ?? '' },
      ])))
      setMessage({ type: 'success', text: 'Delivery slot saved' })
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message })
    } finally {
      setSaving(null)
    }
  }, [slotDrafts])

  const sendTest = useCallback(async () => {
    if (!testRecipient.trim()) return
    setTesting(true)
    setMessage(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiDeliveryConfig & { test?: { gcs_uri?: string | null } }>('/api/admin/usage/service-desk-kpi/delivery/test', {
        method: 'POST',
        body: JSON.stringify({ recipient: testRecipient.trim().toLowerCase() }),
      })
      setConfig(payload)
      setMessage({ type: 'success', text: `Test report sent to ${testRecipient.trim().toLowerCase()}` })
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message })
    } finally {
      setTesting(false)
    }
  }, [testRecipient])

  const schedulerEnabled = config?.scheduler?.enabled
  const deliveryOn = Boolean(config?.enabled && (schedulerEnabled ?? true))
  const statusText = deliveryOn ? 'On' : 'Off'
  const schedulerLabel = config?.scheduler
    ? `${config.scheduler.enabled ? 'Cron active' : 'Cron paused'} · ${config.scheduler.schedule_time ?? config.scheduler.schedule ?? '10:30'}`
    : 'Cron state unavailable'
  const primarySlot = config?.slots?.[0]

  return (
    <div className="rounded-lg border bg-card shadow-sm">
      <div className="flex items-start justify-between gap-3 border-b px-3 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">KPI email delivery</h3>
          <p className="truncate text-[11px] text-muted-foreground">
            Service Analytics · {config?.timezone ?? 'America/New_York'} · {schedulerLabel}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <div className="flex h-8 items-center gap-1.5 rounded-md border bg-muted/20 px-2 text-xs">
            <Switch
              checked={deliveryOn}
              disabled={!config || loading || saving === 'config'}
              onCheckedChange={(checked) => void patchDelivery({ enabled: checked })}
            />
            <span className="font-medium">{statusText}</span>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => void loadDelivery()}
            disabled={loading}
            title="Refresh delivery settings"
          >
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            <span className="sr-only">Refresh delivery settings</span>
          </Button>
        </div>
      </div>

      {message && (
        <div className={`flex items-center gap-1.5 border-b px-3 py-2 text-xs ${
          message.type === 'success' ? 'text-green-600' : 'text-destructive'
        }`}>
          {message.type === 'success' ? <CheckCircle2 className="size-3" /> : <AlertTriangle className="size-3" />}
          <span className="min-w-0 break-words">{message.text}</span>
        </div>
      )}

      <div className="space-y-3 p-3">
        <div className="grid grid-cols-4 gap-1.5 text-xs">
          <div className="rounded-md border bg-muted/20 px-2 py-1.5">
            <div className="text-[10px] font-medium uppercase tracking-normal text-muted-foreground">To</div>
            <div className="font-semibold tabular-nums">{config?.recipients.length ?? 0}</div>
          </div>
          <div className="rounded-md border bg-muted/20 px-2 py-1.5">
            <div className="text-[10px] font-medium uppercase tracking-normal text-muted-foreground">Cc</div>
            <div className="font-semibold tabular-nums">{config?.cc_recipients?.length ?? 0}</div>
          </div>
          <div className="rounded-md border bg-muted/20 px-2 py-1.5">
            <div className="text-[10px] font-medium uppercase tracking-normal text-muted-foreground">Cron</div>
            <div className="font-semibold tabular-nums">{config?.scheduler?.schedule_time ?? primarySlot?.time ?? '10:30'}</div>
          </div>
          <div className="rounded-md border bg-muted/20 px-2 py-1.5">
            <div className="text-[10px] font-medium uppercase tracking-normal text-muted-foreground">Transport</div>
            <div className="truncate font-semibold">{config?.transport ?? 'gmail_api'}</div>
          </div>
        </div>

        <div className="grid min-w-0 gap-3 xl:grid-cols-2">
          <div className="min-w-0 space-y-2">
            <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">To recipients</div>
            <KpiRecipientManager
              label="To"
              emails={configuredRecipients}
              filteredEmails={filteredRecipients}
              search={recipientSearch}
              onSearchChange={setRecipientSearch}
              newEmail={newRecipient}
              onNewEmailChange={setNewRecipient}
              bulkValue={bulkRecipients}
              onBulkChange={setBulkRecipients}
              parsedBulkCount={parsedBulkRecipients.length}
              bulkNewCount={bulkRecipientStats.next.length}
              bulkDuplicateCount={bulkRecipientStats.duplicateCount}
              loading={loading}
              saving={saving}
              addSavingKey="recipient:add"
              itemSavingPrefix="recipient"
              onAddOne={() => void addRecipient()}
              onRemoveOne={(email) => void removeRecipient(email)}
              onAddBulk={() => void addBulkRecipients()}
            />
          </div>
          <div className="min-w-0 space-y-2">
            <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">CC recipients</div>
            <KpiRecipientManager
              label="Cc"
              emails={configuredCcRecipients}
              filteredEmails={filteredCcRecipients}
              search={ccRecipientSearch}
              onSearchChange={setCcRecipientSearch}
              newEmail={newCcRecipient}
              onNewEmailChange={setNewCcRecipient}
              bulkValue={bulkCcRecipients}
              onBulkChange={setBulkCcRecipients}
              parsedBulkCount={parsedBulkCcRecipients.length}
              bulkNewCount={bulkCcRecipientStats.next.length}
              bulkDuplicateCount={bulkCcRecipientStats.duplicateCount}
              loading={loading}
              saving={saving}
              addSavingKey="cc_recipient:add"
              itemSavingPrefix="cc_recipient"
              onAddOne={() => void addCcRecipient()}
              onRemoveOne={(email) => void removeCcRecipient(email)}
              onAddBulk={() => void addBulkCcRecipients()}
            />
          </div>
        </div>

        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)]">
          <div className="min-w-0 space-y-2">
            <div className="flex items-center justify-between gap-3">
              <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">Daily cron time</div>
              <Clock3 className="size-3.5 text-muted-foreground" />
            </div>
            {loading ? (
              <div className="flex min-h-10 items-center gap-2 rounded-md border px-2 py-2 text-xs text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                Loading cron time...
              </div>
            ) : !config || !primarySlot ? (
              <div className="rounded-md border px-2 py-2 text-xs text-muted-foreground">No delivery time configured.</div>
            ) : (() => {
              const slot = primarySlot
              const draft = slotDrafts[slot.id] ?? { time: slot.time, label: slot.label ?? '' }
              const busy = saving === `slot:${slot.id}`
              const changed = draft.time !== slot.time || draft.label !== (slot.label ?? '')
              return (
                <div className="grid gap-1.5 rounded-md border p-1.5 sm:grid-cols-[auto_6rem_minmax(0,1fr)_auto] sm:items-center">
                  <Switch
                    checked={slot.enabled}
                    disabled={busy}
                    onCheckedChange={(checked) => void saveSlot(slot, { enabled: checked })}
                  />
                  <Input
                    type="time"
                    step={900}
                    value={draft.time}
                    onChange={(event) => setSlotDrafts(prev => ({ ...prev, [slot.id]: { ...draft, time: event.target.value } }))}
                    className="h-7 w-full px-2 text-xs"
                  />
                  <Input
                    value={draft.label}
                    onChange={(event) => setSlotDrafts(prev => ({ ...prev, [slot.id]: { ...draft, label: event.target.value } }))}
                    placeholder="Label"
                    className="h-7 min-w-0 px-2 text-xs"
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 w-full px-2 sm:w-7 sm:p-0"
                    disabled={busy || !changed}
                    onClick={() => void saveSlot(slot)}
                    title="Save cron time"
                  >
                    {busy ? <Loader2 className="size-3 animate-spin" /> : <Save className="size-3" />}
                    <span className="sm:hidden">Save</span>
                  </Button>
                </div>
              )
            })()}
          </div>

          <div className="space-y-2">
            <div className="space-y-1.5 rounded-md border bg-muted/20 p-2">
              <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">Safe test</div>
              <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-1.5">
                <Input
                  value={testRecipient}
                  onChange={(event) => setTestRecipient(event.target.value)}
                  placeholder="morgan.chen@northstar.example.com"
                  className="h-8 min-w-0 text-xs"
                />
                <Button variant="outline" size="sm" className="h-8 shrink-0 px-2" onClick={() => void sendTest()} disabled={testing || loading}>
                  {testing ? <Loader2 className="size-3 animate-spin" /> : <Bell className="size-3" />}
                  Test
                </Button>
              </div>
            </div>

            {config?.last_test && (
              <div className="rounded-md border bg-muted/20 px-2 py-1.5 text-xs text-muted-foreground">
                Last test: {String(config.last_test.recipient ?? 'safe recipient')} · {String(config.last_test.report_date ?? 'latest report')}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function ServiceDeskKpiPanel() {
  const [searchParams, setSearchParams] = useSearchParams()
  const defaultReportDate = useMemo(() => dateInputValueInBusinessTime(-1), [])
  const urlStartDate = searchParams.get('from') ?? searchParams.get('start_date') ?? searchParams.get('date')
  const urlEndDate = searchParams.get('to') ?? searchParams.get('end_date') ?? searchParams.get('date')
  const initialStartDate = isDateInputValue(urlStartDate) ? urlStartDate : defaultReportDate
  const initialEndDate = isDateInputValue(urlEndDate) ? urlEndDate : initialStartDate
  const [startDate, setStartDate] = useState(initialStartDate)
  const [endDate, setEndDate] = useState(initialEndDate)
  const [reports, setReports] = useState<ServiceDeskKpiReport[]>([])
  const [snapshotFiles, setSnapshotFiles] = useState<ServiceDeskKpiSnapshotFile[]>([])
  const [loading, setLoading] = useState(true)
  const [snapshotsLoading, setSnapshotsLoading] = useState(true)
  const [exporting, setExporting] = useState<'pdf' | 'xlsx' | null>(null)
  const [downloadingSnapshotDate, setDownloadingSnapshotDate] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [snapshotError, setSnapshotError] = useState<string | null>(null)
  const [kpiTableSearch, setKpiTableSearch] = useState('')
  const [kpiTableSort, setKpiTableSort] = useState<ServiceDeskKpiTableSort>(null)
  const reportDates = useMemo(() => {
    const start = new Date(`${startDate}T12:00:00`)
    const end = new Date(`${endDate}T12:00:00`)
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return []
    const first = start <= end ? start : end
    const last = start <= end ? end : start
    const days: string[] = []
    const cursor = new Date(first)
    while (cursor <= last && days.length < SERVICE_DESK_KPI_MAX_RANGE_DAYS + 1) {
      days.push(cursor.toISOString().slice(0, 10))
      cursor.setDate(cursor.getDate() + 1)
    }
    return days
  }, [startDate, endDate])
  const data = reports[reports.length - 1] ?? null
  const reportTitle = useMemo(() => {
    if (reportDates.length === 0) return 'Service Desk KPI'
    if (reportDates.length === 1) return `KPI Report for ${formatReportDate(reportDates[0])}`
    return `KPI Report for ${formatReportDate(reportDates[0])} - ${formatReportDate(reportDates[reportDates.length - 1])}`
  }, [reportDates])
  const reportDateLabel = useMemo(() => {
    if (reportDates.length === 0) return ''
    if (reportDates.length === 1) return formatReportDate(reportDates[0])
    return `${formatReportDate(reportDates[0])} - ${formatReportDate(reportDates[reportDates.length - 1])}`
  }, [reportDates])

  useEffect(() => {
    const nextStartDate = isDateInputValue(urlStartDate) ? urlStartDate : defaultReportDate
    const nextEndDate = isDateInputValue(urlEndDate) ? urlEndDate : nextStartDate
    if (nextStartDate !== startDate) setStartDate(nextStartDate)
    if (nextEndDate !== endDate) setEndDate(nextEndDate)
  }, [defaultReportDate, endDate, startDate, urlEndDate, urlStartDate])

  const updateDateRange = useCallback((nextStartDate: string, nextEndDate: string) => {
    setStartDate(nextStartDate)
    setEndDate(nextEndDate)
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('from', nextStartDate)
    nextParams.set('to', nextEndDate)
    nextParams.delete('date')
    nextParams.delete('start_date')
    nextParams.delete('end_date')
    setSearchParams(nextParams, { replace: true })
  }, [searchParams, setSearchParams])

  const load = useCallback(async (refreshTelephony = false) => {
    setLoading(true)
    setError(null)
    try {
      if (reportDates.length === 0) throw new Error('Select a valid date range')
      if (reportDates.length > SERVICE_DESK_KPI_MAX_RANGE_DAYS) {
        throw new Error(`Select ${SERVICE_DESK_KPI_MAX_RANGE_DAYS} days or fewer`)
      }
      if (refreshTelephony) {
        // Refresh applies to every day in the selected range.
      }
      const results = await Promise.all(reportDates.map(day => {
        const params = new URLSearchParams()
        params.set('date', day)
        if (refreshTelephony) params.set('refresh_telephony', '1')
        return apiFetch<ServiceDeskKpiReport>(`/api/admin/usage/service-desk-kpi?${params.toString()}`)
      }))
      setReports(results)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [reportDates])

  useEffect(() => { void load(false) }, [load])

  const loadSnapshots = useCallback(async () => {
    setSnapshotsLoading(true)
    setSnapshotError(null)
    try {
      const payload = await apiFetch<ServiceDeskKpiSnapshotList>('/api/admin/usage/service-desk-kpi/snapshots?limit=60')
      setSnapshotFiles(payload.items ?? [])
    } catch (e) {
      setSnapshotError((e as Error).message)
    } finally {
      setSnapshotsLoading(false)
    }
  }, [])

  useEffect(() => { void loadSnapshots() }, [loadSnapshots])

  const downloadExport = useCallback(async (format: 'pdf' | 'xlsx') => {
    if (!data) return
    setExporting(format)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (reportDates.length === 1 && format === 'xlsx') {
        params.set('date', reportDates[0])
      } else {
        params.set('start_date', reportDates[0])
        params.set('end_date', reportDates[reportDates.length - 1])
      }
      params.set('format', format)
      const res = await fetch(`/api/admin/usage/service-desk-kpi/export?${params.toString()}`, {
        headers: await authHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`)
      }
      const blob = await res.blob()
      const fallbackRange = reportDates.length === 1 ? reportDates[0] : `${reportDates[0]}_to_${reportDates[reportDates.length - 1]}`
      const fallback = `service-desk-kpi-${fallbackRange}.${format}`
      const filename = downloadFilename(res.headers.get('Content-Disposition'), fallback)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExporting(null)
    }
  }, [data, reportDates])

  const downloadSnapshot = useCallback(async (item: ServiceDeskKpiSnapshotFile) => {
    setDownloadingSnapshotDate(item.report_date)
    setSnapshotError(null)
    try {
      const res = await fetch(`/api/admin/usage/service-desk-kpi/snapshots/${item.report_date}/download`, {
        headers: await authHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`)
      }
      const blob = await res.blob()
      const filename = downloadFilename(res.headers.get('Content-Disposition'), item.filename)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setSnapshotError((e as Error).message)
    } finally {
      setDownloadingSnapshotDate(null)
    }
  }, [])

  const chartRows = useMemo(
    () => (data?.rows ?? [])
      .filter(row => row.new_assigned_tickets || row.solved_tickets || row.outstanding_tickets)
      .slice(0, 10)
      .map(row => ({
        name: shortPersonLabel(row, 20),
        assigned: row.new_assigned_tickets,
        solved: row.solved_tickets,
        outstanding: row.outstanding_tickets,
      })),
    [data],
  )

  const groupedReportRows = useMemo(() => {
    const summaryAsRow = (report: ServiceDeskKpiReport | null): ServiceDeskKpiMetricRow | undefined => {
      if (!report) return undefined
      return {
        email: '__report_total__',
        display_name: 'Report total',
        inbound_calls: report.summary.inbound_calls,
        avg_call_time_seconds: report.summary.avg_call_time_seconds,
        new_assigned_tickets: report.summary.new_assigned_tickets,
        same_day_solved_tickets: report.summary.same_day_solved_tickets,
        solved_tickets: report.summary.solved_tickets,
        outstanding_tickets: report.summary.outstanding_tickets,
        median_resolution_hours: null,
        single_cycle_solved: report.summary.single_cycle_solved,
        single_cycle_total: report.summary.single_cycle_total,
        single_cycle_solve_rate_pct: report.summary.single_cycle_solve_rate_pct,
        sla_breached: report.summary.sla_breached,
        sla_known: report.summary.sla_known,
        sla_breach_rate_pct: report.summary.sla_breach_rate_pct,
        first_reply_tickets: 0,
        median_first_public_reply_hours: report.summary.median_first_public_reply_hours,
        customer_reply_count: report.summary.customer_replies,
        median_customer_reply_hours: report.summary.median_customer_reply_hours,
      }
    }

    const out: ServiceDeskKpiGroupedItem[] = [{
      key: '__report_total__',
      groupLabel: 'Total',
      assigneeLabel: 'Report total',
      rowType: 'report_total',
      byDate: Object.fromEntries(reports.map(report => [report.date, summaryAsRow(report)])),
    }]

    const sectionsByDate = new Map(reports.map(report => [
      report.date,
      new Map((report.group_sections ?? []).map(section => [section.group_key, section])),
    ]))
    const groupKeys: string[] = []
    for (const report of reports) {
      for (const section of report.group_sections ?? []) {
        if (!groupKeys.includes(section.group_key)) groupKeys.push(section.group_key)
      }
    }

    for (const groupKey of groupKeys) {
      const groupName = sectionNameForKey(groupKey, reports, sectionsByDate)
      out.push({
        key: `group-total:${groupKey}`,
        groupLabel: groupName,
        assigneeLabel: 'Group total',
        rowType: 'group_total',
        byDate: Object.fromEntries(reports.map(report => [
          report.date,
          sectionsByDate.get(report.date)?.get(groupKey)?.total,
        ])),
      })

      const rowsByDate = new Map(reports.map(report => [
        report.date,
        new Map((sectionsByDate.get(report.date)?.get(groupKey)?.rows ?? []).map(row => [row.email, row])),
      ]))
      const rowKeys: string[] = []
      for (const report of reports) {
        for (const row of sectionsByDate.get(report.date)?.get(groupKey)?.rows ?? []) {
          if (!rowKeys.includes(row.email)) rowKeys.push(row.email)
        }
      }

      for (const key of rowKeys) {
        const row = firstRowForKey(key, reports, rowsByDate)
        if (!row) continue
        out.push({
          key: `${groupKey}:${key}`,
          groupLabel: '',
          assigneeLabel: personLabel(row),
          rowType: row.row_type === 'queue' ? 'queue' : 'assignee',
          byDate: Object.fromEntries(reports.map(report => [
            report.date,
            rowsByDate.get(report.date)?.get(key),
          ])),
        })
      }
    }

    return out
  }, [reports])

  const visibleGroupedReportRows = useMemo(() => {
    const query = kpiTableSearch.trim().toLocaleLowerCase()
    const reportTotal = groupedReportRows.find(item => item.rowType === 'report_total')
    const groups: Array<{ total: ServiceDeskKpiGroupedItem; rows: ServiceDeskKpiGroupedItem[] }> = []
    for (const item of groupedReportRows) {
      if (item.rowType === 'report_total') continue
      if (item.rowType === 'group_total') {
        groups.push({ total: item, rows: [] })
        continue
      }
      groups[groups.length - 1]?.rows.push(item)
    }

    const filteredGroups = groups.flatMap(group => {
      if (!query) return [group]
      const groupMatch = group.total.groupLabel.toLocaleLowerCase().includes(query)
      const matchingRows = groupMatch
        ? group.rows
        : group.rows.filter(row => (
          row.assigneeLabel.toLocaleLowerCase().includes(query)
          || row.groupLabel.toLocaleLowerCase().includes(query)
        ))
      return groupMatch || matchingRows.length > 0 ? [{ total: group.total, rows: matchingRows }] : []
    })

    const sortedGroups = [...filteredGroups]
    if (kpiTableSort?.field === 'group') {
      sortedGroups.sort((left, right) => compareServiceDeskKpiItems(left.total, right.total, kpiTableSort))
    }

    const out: ServiceDeskKpiGroupedItem[] = query || !reportTotal ? [] : [reportTotal]
    for (const group of sortedGroups) {
      const sortedRows = [...group.rows]
      if (kpiTableSort && kpiTableSort.field !== 'group') {
        sortedRows.sort((left, right) => compareServiceDeskKpiItems(left, right, kpiTableSort))
      }
      out.push(group.total, ...sortedRows)
    }
    return out
  }, [groupedReportRows, kpiTableSearch, kpiTableSort])

  const kpiCount = (
    row: ServiceDeskKpiMetricRow | undefined,
    key: 'inbound_calls' | 'new_assigned_tickets' | 'same_day_solved_tickets' | 'solved_tickets' | 'outstanding_tickets',
  ) => <span className="tabular-nums">{(row?.[key] ?? 0).toLocaleString()}</span>

  const kpiCallTime = (row: ServiceDeskKpiMetricRow | undefined) => (
    <span className="tabular-nums">{fmtSeconds(row?.avg_call_time_seconds)}</span>
  )

  const kpiFcr = (row: ServiceDeskKpiMetricRow | undefined) => {
    if (!row || row.single_cycle_total <= 0) return <span className="text-muted-foreground">—</span>
    return <span className="tabular-nums">{row.single_cycle_solve_rate_pct}%</span>
  }

  const setRangeDays = (days: number) => {
    updateDateRange(addDaysToDateInput(defaultReportDate, -(days - 1)), defaultReportDate)
  }

  const rangeTooLong = reportDates.length > SERVICE_DESK_KPI_MAX_RANGE_DAYS
  const tableColSpan = 2 + reports.length * 6
  const metricHeaders = ['Inbound\nCalls', 'Avg Call\nTime', 'Assigned', 'Solved', 'Outstanding', 'FCR\nproxy']
  const metricSortFields: ServiceDeskKpiTableSortField[] = [
    'inbound_calls',
    'avg_call_time_seconds',
    'new_assigned_tickets',
    'same_day_solved_tickets',
    'outstanding_tickets',
    'single_cycle_solve_rate_pct',
  ]
  const metricSortLabels: Record<ServiceDeskKpiTableSortField, string> = {
    group: 'Groups',
    assignee: 'Assignee',
    inbound_calls: 'Inbound Calls',
    avg_call_time_seconds: 'Avg Call Time',
    new_assigned_tickets: 'Assigned',
    same_day_solved_tickets: 'Solved',
    outstanding_tickets: 'Outstanding',
    single_cycle_solve_rate_pct: 'FCR proxy',
  }
  const separatorRight = '[border-right-style:double] border-r-[3px] border-r-black'
  const separatorLeft = '[border-left-style:double] border-l-[3px] border-l-black'
  const dateRightBorder = (dateIndex: number) => dateIndex < reports.length - 1 ? separatorRight : ''
  const metricCellClass = (dateIndex: number, metricIndex: number, extra = '') =>
    `border border-[#d8dee8] px-1 py-1 text-center ${metricIndex === 0 && dateIndex === 0 ? separatorLeft : ''} ${metricIndex === 5 ? dateRightBorder(dateIndex) : ''} ${extra}`.trim()
  const updateKpiTableSort = (field: ServiceDeskKpiTableSortField, date?: string) => {
    setKpiTableSort(current => {
      if (current?.field === field && current.date === date) {
        return { field, date, direction: current.direction === 'asc' ? 'desc' : 'asc' }
      }
      return { field, date, direction: defaultServiceDeskKpiSortDirection(field) }
    })
  }
  const kpiSortDescription = kpiTableSort
    ? `Sorted by ${metricSortLabels[kpiTableSort.field]}${kpiTableSort.date ? ` on ${formatKpiColumnDate(kpiTableSort.date)}` : ''}, ${kpiTableSort.direction === 'asc' ? 'ascending' : 'descending'}.`
    : 'Default order matches the report. Metric sorting keeps group sections intact.'
  const renderSortIcon = (field: ServiceDeskKpiTableSortField, date?: string) => {
    const active = kpiTableSort?.field === field && kpiTableSort.date === date
    if (!active) return <ChevronDown className="size-3 opacity-25" />
    return kpiTableSort.direction === 'asc'
      ? <ChevronUp className="size-3" />
      : <ChevronDown className="size-3" />
  }
  const sortableHeaderButton = (
    field: ServiceDeskKpiTableSortField,
    label: ReactNode,
    date?: string,
    align: 'left' | 'center' = 'center',
  ) => {
    const active = kpiTableSort?.field === field && kpiTableSort.date === date
    return (
      <button
        type="button"
        className={`inline-flex w-full items-center gap-1 rounded px-1 py-0.5 text-xs font-bold hover:bg-white/40 focus:outline-none focus:ring-2 focus:ring-slate-500 ${align === 'center' ? 'justify-center' : 'justify-start'} ${active ? 'text-slate-950' : 'text-slate-800'}`}
        onClick={() => updateKpiTableSort(field, date)}
        title={`Sort by ${metricSortLabels[field]}${date ? ` on ${formatKpiColumnDate(date)}` : ''}`}
      >
        <span>{label}</span>
        {renderSortIcon(field, date)}
      </button>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-3 rounded-lg border bg-card p-3 shadow-sm lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">
            {reportTitle}
          </h3>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            From
            <Input
              type="date"
              className="h-9 w-[11rem]"
              value={startDate}
              onChange={(e) => updateDateRange(e.target.value, endDate)}
            />
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            To
            <Input
              type="date"
              className="h-9 w-[11rem]"
              value={endDate}
              onChange={(e) => updateDateRange(startDate, e.target.value)}
            />
          </label>
          <div className="flex h-9 items-center gap-1">
            <Button type="button" variant="outline" size="sm" onClick={() => setRangeDays(1)}>1 day</Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setRangeDays(2)}>2 days</Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setRangeDays(7)}>7 days</Button>
          </div>
          <Button variant="outline" size="sm" onClick={() => void load(true)} disabled={loading}>
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            Refresh
          </Button>
          <Button variant="default" size="sm" onClick={() => void downloadExport('pdf')} disabled={!data || rangeTooLong || Boolean(exporting)}>
            {exporting === 'pdf' ? <Loader2 className="size-4 animate-spin" /> : <Printer className="size-4" />}
            Report PDF
          </Button>
          <Button variant="outline" size="sm" onClick={() => void downloadExport('xlsx')} disabled={!data || rangeTooLong || Boolean(exporting)}>
            {exporting === 'xlsx' ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
            Excel
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {rangeTooLong && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          Select {SERVICE_DESK_KPI_MAX_RANGE_DAYS} days or fewer.
        </div>
      )}

      <div className="grid gap-3">
        <div className="min-w-0 rounded-lg border bg-card shadow-sm">
          <div className="flex flex-col gap-2 border-b px-3 py-2.5 md:flex-row md:items-center md:justify-between">
            <div className="min-w-0">
              <h3 className="text-sm font-semibold">Available daily snapshots</h3>
              <p className="truncate text-xs text-muted-foreground">
                Daily Excel files · available after the configured delivery time
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="h-8 w-8 shrink-0 p-0"
              onClick={() => void loadSnapshots()}
              disabled={snapshotsLoading}
              title="Refresh snapshot files"
            >
              {snapshotsLoading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              <span className="sr-only">Refresh snapshot files</span>
            </Button>
          </div>
          <div className="p-3">
            {snapshotError && (
              <div className="mb-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {snapshotError}
              </div>
            )}
            {snapshotsLoading && snapshotFiles.length === 0 ? (
              <div className="flex min-h-[8rem] items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
                <Loader2 className="mr-2 size-4 animate-spin" />
                Loading snapshot files...
              </div>
            ) : snapshotFiles.length === 0 ? (
              <div className="rounded-md border border-dashed px-4 py-6 text-sm text-muted-foreground">
                No daily snapshot files found yet.
              </div>
            ) : (
              <div className="max-h-[18rem] overflow-auto rounded-md border">
                <table className="w-full min-w-[620px] text-sm">
                  <thead className="sticky top-0 z-10 border-b bg-muted text-xs text-muted-foreground">
                    <tr>
                      <th className="w-[12rem] px-3 py-1.5 text-left font-medium">Report</th>
                      <th className="px-3 py-1.5 text-left font-medium">File</th>
                      <th className="w-[5rem] px-3 py-1.5 text-right font-medium">Size</th>
                      <th className="w-[3.5rem] px-3 py-1.5 text-right font-medium">Get</th>
                    </tr>
                  </thead>
                  <tbody>
                    {snapshotFiles.map(item => (
                      <tr key={item.report_date} className="border-b last:border-b-0">
                        <td className="px-3 py-1.5 align-middle">
                          <div className="font-medium">{formatReportDate(item.report_date)}</div>
                          <div className="text-xs text-muted-foreground">{formatDateTimeShort(item.updated_at)}</div>
                        </td>
                        <td className="min-w-0 px-3 py-1.5 align-middle">
                          <div className="truncate font-medium">{item.filename}</div>
                          <div className="truncate text-[11px] text-muted-foreground">{item.blob_name}</div>
                        </td>
                        <td className="px-3 py-1.5 text-right align-middle tabular-nums">{formatFileSize(item.size_bytes)}</td>
                        <td className="px-3 py-1.5 text-right align-middle">
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-8 w-8 p-0"
                            onClick={() => void downloadSnapshot(item)}
                            disabled={downloadingSnapshotDate === item.report_date}
                            title={`Download ${item.filename}`}
                          >
                            {downloadingSnapshotDate === item.report_date ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
                            <span className="sr-only">Download {item.filename}</span>
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      {loading && !data ? (
        <div className="flex min-h-[18rem] items-center justify-center rounded-lg border bg-card">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : data && (
        <>
          <div className="grid gap-2 md:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
            <UsageMetricCard
              title="Active owners"
              value={data.summary.active_engineers.toLocaleString()}
              detail="People or queues with ticket work or backlog"
              icon={<UsersRound className="size-4" />}
              compact
            />
            <UsageMetricCard
              title="Inbound calls"
              value={data.summary.inbound_calls.toLocaleString()}
              detail={`Inbound calls, avg ${fmtSeconds(data.summary.avg_call_time_seconds)}`}
              icon={<Activity className="size-4" />}
              compact
            />
            <UsageMetricCard
              title="Assigned"
              value={data.summary.new_assigned_tickets.toLocaleString()}
              detail="Created that day, snapshot owner"
              icon={<Activity className="size-4" />}
              compact
            />
            <UsageMetricCard
              title="Solved"
              value={data.summary.same_day_solved_tickets.toLocaleString()}
              detail="Status changed to Solved that day"
              icon={<CheckCircle2 className="size-4" />}
              compact
            />
            <UsageMetricCard
              title="Outstanding"
              value={data.summary.outstanding_tickets.toLocaleString()}
              detail="New, open, pending, or hold at snapshot"
              icon={<Database className="size-4" />}
              compact
            />
            <UsageMetricCard
              title="Reply time"
              value={fmtHours(data.summary.median_customer_reply_hours)}
              detail={`${data.summary.customer_replies.toLocaleString()} customer-to-agent replies`}
              icon={<Clock3 className="size-4" />}
              compact
            />
            <UsageMetricCard
              title="Single-cycle"
              value={`${data.summary.single_cycle_solve_rate_pct}%`}
              detail={`FCR proxy, ${fmtCountFraction(data.summary.single_cycle_solved, data.summary.single_cycle_total, 'solved tickets')}`}
              icon={<CheckCircle2 className="size-4" />}
              compact
            />
          </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="flex flex-col gap-2 border-b p-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold">
                  Service Desk KPI by group and assignee{reportDateLabel ? ` — ${reportDateLabel}` : ''}
                </h3>
                <p className="mt-0.5 text-xs text-muted-foreground">{kpiSortDescription}</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={kpiTableSearch}
                    onChange={(event) => setKpiTableSearch(event.target.value)}
                    placeholder="Search group or assignee"
                    className="h-8 w-[15rem] pl-7 text-xs"
                  />
                </div>
                {(kpiTableSearch || kpiTableSort) && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8"
                    onClick={() => {
                      setKpiTableSearch('')
                      setKpiTableSort(null)
                    }}
                  >
                    Reset
                  </Button>
                )}
              </div>
            </div>
            <div className="overflow-x-auto p-3">
              <table className="w-full border-collapse text-xs" style={{ minWidth: `${Math.max(780, 280 + reports.length * 430)}px` }}>
                <thead>
                  <tr className="bg-[#cfc3df] text-slate-950">
                    <th className="border border-[#d8dee8] px-2 py-1 text-center font-bold" colSpan={2}>Date</th>
                    {reports.map((report, dateIndex) => (
                      <th
                        key={report.date}
                        className={`border border-[#d8dee8] px-2 py-1 text-center font-bold ${dateIndex === 0 ? separatorLeft : ''} ${dateRightBorder(dateIndex)}`}
                        colSpan={6}
                      >
                        {formatKpiColumnDate(report.date)}
                      </th>
                    ))}
                  </tr>
                  <tr className="[border-bottom-style:double] border-b-[3px] border-b-black bg-[#e2e8f0] text-slate-950">
                    <th className="w-[10rem] border border-[#d8dee8] px-1 py-1 text-left font-bold">
                      {sortableHeaderButton('group', 'Groups', undefined, 'left')}
                    </th>
                    <th className="w-[13rem] border border-[#d8dee8] px-1 py-1 text-left font-bold">
                      {sortableHeaderButton('assignee', 'Assignee', undefined, 'left')}
                    </th>
                    {reports.map((report, dateIndex) => metricHeaders.map((header, metricIndex) => (
                      <th
                        key={`${report.date}:${header}`}
                        className={metricCellClass(dateIndex, metricIndex, 'font-bold text-slate-950')}
                      >
                        {sortableHeaderButton(
                          metricSortFields[metricIndex],
                          <>{header.split('\n').map(part => <span key={part} className="block">{part}</span>)}</>,
                          report.date,
                        )}
                      </th>
                    )))}
                  </tr>
                </thead>
                <tbody>
                  {visibleGroupedReportRows.length === 0 ? (
                    <tr>
                      <td className="border border-border px-2 py-5 text-center text-muted-foreground" colSpan={tableColSpan}>
                        {kpiTableSearch ? 'No KPI rows match this search.' : 'No KPI rows for this date range.'}
                      </td>
                    </tr>
                  ) : visibleGroupedReportRows.map((item, index) => {
                    const reportTotal = item.rowType === 'report_total'
                    const groupTotal = item.rowType === 'group_total'
                    const rowClass = reportTotal
                      ? 'bg-[#244c6d] font-semibold text-white'
                      : groupTotal
                        ? '[border-top-style:double] border-t-[3px] border-t-black bg-[#eef3f8] font-semibold text-slate-950'
                        : (index % 2 === 0 ? 'bg-[#f9fbfd]' : 'bg-background')
                    return (
                    <tr key={item.key} className={rowClass}>
                      <td className="border border-[#d8dee8] px-2 py-1 font-semibold">{item.groupLabel}</td>
                      <td className="border border-[#d8dee8] px-2 py-1">{item.assigneeLabel}</td>
                      {reports.map((report, dateIndex) => {
                        const row = item.byDate[report.date]
                        return (
                          <Fragment key={`${item.key}:${report.date}`}>
                            <td className={metricCellClass(dateIndex, 0)}>{kpiCount(row, 'inbound_calls')}</td>
                            <td className={metricCellClass(dateIndex, 1)}>{kpiCallTime(row)}</td>
                            <td className={metricCellClass(dateIndex, 2)}>{kpiCount(row, 'new_assigned_tickets')}</td>
                            <td className={metricCellClass(dateIndex, 3)}>{kpiCount(row, 'same_day_solved_tickets')}</td>
                            <td className={metricCellClass(dateIndex, 4)}>{kpiCount(row, 'outstanding_tickets')}</td>
                            <td className={metricCellClass(dateIndex, 5)}>{kpiFcr(row)}</td>
                          </Fragment>
                        )
                      })}
                    </tr>
                    )
                  })}
                </tbody>
              </table>
              {(data.grouping?.warnings ?? []).length > 0 && (
                <div className="mt-3 grid gap-2 text-xs text-muted-foreground">
                  {(data.grouping?.warnings ?? []).map(item => (
                    <div key={item}>{item}</div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <h3 className="text-sm font-semibold">Daily workload by engineer / queue</h3>
              <p className="mb-3 text-xs text-muted-foreground">Top rows by tickets solved that day and reconstructed backlog.</p>
              <div className="h-[290px]">
                {chartRows.length === 0 ? (
                  <div className="flex h-full items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
                    No Zendesk ticket KPI rows for this date.
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={chartRows} layout="vertical" margin={{ left: 22, right: 12 }} barCategoryGap={10}>
                      <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                      <YAxis type="category" dataKey="name" width={125} tick={{ fontSize: 11 }} />
                      <Tooltip />
                      <Legend />
                      <Bar dataKey="assigned" name="Assigned" fill="#2563eb" radius={[0, 3, 3, 0]} barSize={16} />
                      <Bar dataKey="solved" name="Solved" fill="#16a34a" radius={[0, 3, 3, 0]} barSize={16} />
                      <Bar dataKey="outstanding" name="Outstanding" fill="#f97316" radius={[0, 3, 3, 0]} barSize={16} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>

            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <h3 className="text-sm font-semibold">Data coverage</h3>
              <p className="mb-3 text-xs text-muted-foreground">Zendesk metadata coverage and CXone phone cache status for this report.</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-md border bg-muted/20 p-3">
                  <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">Assignee email coverage</div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums">{data.coverage.assignee_email_coverage_pct}%</div>
                  <div className="text-xs text-muted-foreground">
                    {data.coverage.tickets_with_internal_assignee_email.toLocaleString()} of {data.coverage.relevant_tickets.toLocaleString()} relevant tickets
                  </div>
                </div>
                <div className="rounded-md border bg-muted/20 p-3">
                  <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">Reply author coverage</div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums">{data.coverage.comment_author_email_coverage_pct}%</div>
                  <div className="text-xs text-muted-foreground">
                    {data.coverage.public_agent_replies_with_email.toLocaleString()} of {data.coverage.public_agent_replies.toLocaleString()} public agent replies
                  </div>
                </div>
                <div className="rounded-md border bg-muted/20 p-3">
                  <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">CXone phone contacts</div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums">{(data.coverage.telephony?.contact_count ?? 0).toLocaleString()}</div>
                  <div className="text-xs text-muted-foreground">
                    {data.coverage.telephony?.available ? `Status: ${data.coverage.telephony.status ?? 'loaded'}` : 'Unavailable for this report'}
                  </div>
                </div>
              </div>
              <div className="mt-4 space-y-2 text-sm">
                {data.warnings.length > 0 && data.warnings.map(item => (
                  <div key={item} className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
                    {item}
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="border-b p-4">
              <h3 className="text-sm font-semibold">Daily rows</h3>
              <p className="text-xs text-muted-foreground">
                Counts are grouped by reconstructed snapshot owner unless the column says reply author.
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1100px] text-sm">
                <thead className="border-b bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium">Engineer / queue</th>
                    <th className="px-4 py-2 text-right font-medium">Inbound calls</th>
                    <th className="px-4 py-2 text-right font-medium">Avg call time</th>
                    <th className="px-4 py-2 text-right font-medium">Assigned</th>
                    <th className="px-4 py-2 text-right font-medium">Solved</th>
                    <th className="px-4 py-2 text-right font-medium">Outstanding</th>
                    <th className="px-4 py-2 text-right font-medium">First public reply</th>
                    <th className="px-4 py-2 text-right font-medium">Customer reply by author</th>
                    <th className="px-4 py-2 text-right font-medium">Solved ticket age</th>
                    <th className="px-4 py-2 text-right font-medium">Single-cycle</th>
                    <th className="px-4 py-2 text-right font-medium">SLA breach</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.length === 0 ? (
                    <tr>
                      <td className="px-4 py-8 text-center text-muted-foreground" colSpan={11}>
                        No KPI rows for this date.
                      </td>
                    </tr>
                  ) : data.rows.map(row => (
                    <tr key={row.email} className="border-b last:border-b-0">
                      <td className="px-4 py-2 align-middle">
                        <div className="font-medium">{personLabel(row)}</div>
                        {personSecondaryLabel(row) && <div className="text-xs text-muted-foreground">{personSecondaryLabel(row)}</div>}
                      </td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">{row.inbound_calls.toLocaleString()}</td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">{fmtSeconds(row.avg_call_time_seconds)}</td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">{row.new_assigned_tickets.toLocaleString()}</td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">{row.solved_tickets.toLocaleString()}</td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">{row.outstanding_tickets.toLocaleString()}</td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">
                        <div>{fmtHours(row.median_first_public_reply_hours)}</div>
                        <div className="text-xs text-muted-foreground">{row.first_reply_tickets.toLocaleString()} tickets</div>
                      </td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">
                        <div>{fmtHours(row.median_customer_reply_hours)}</div>
                        <div className="text-xs text-muted-foreground">{row.customer_reply_count.toLocaleString()} replies</div>
                      </td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">{fmtHours(row.median_resolution_hours)}</td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">
                        <div>{row.single_cycle_solve_rate_pct}%</div>
                        <div className="text-xs text-muted-foreground">
                          {fmtCountFraction(row.single_cycle_solved, row.single_cycle_total, 'solved tickets')}
                        </div>
                      </td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">
                        <div>{(row.sla_known || 0) > 0 ? `${row.sla_breach_rate_pct}%` : '—'}</div>
                        <div className="text-xs text-muted-foreground">
                          {fmtCountFraction(row.sla_breached, row.sla_known, 'known SLA tickets')}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function UsageInsightsTab({
  isSuperAdmin,
  initialMode = 'esip',
}: {
  isSuperAdmin: boolean
  initialMode?: InsightMode
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const [insightMode, setInsightMode] = useState<InsightMode>(initialMode)
  const defaultUsageEndDate = useMemo(() => dateInputValueInBusinessTime(-1), [])
  const defaultUsageStartDate = useMemo(() => addDaysToDateInput(defaultUsageEndDate, -6), [defaultUsageEndDate])
  const usageDateParams = useMemo(() => {
    const params = new URLSearchParams(location.search)
    const start = params.get('start_date')
    const end = params.get('end_date')
    return {
      startDate: isDateInputValue(start) ? start : defaultUsageStartDate,
      endDate: isDateInputValue(end) ? end : defaultUsageEndDate,
    }
  }, [defaultUsageEndDate, defaultUsageStartDate, location.search])
  const usageStartDate = usageDateParams.startDate
  const usageEndDate = usageDateParams.endDate
  const [userEmail, setUserEmail] = useState('')
  const [questionType, setQuestionType] = useState('')
  const [comparePrevious, setComparePrevious] = useState(true)
  const [groupBy, setGroupBy] = useState('user')
  const [minActivity, setMinActivity] = useState('0')
  const [problemSignal, setProblemSignal] = useState('all')
  const [categoryFocus, setCategoryFocus] = useState('all')
  const [data, setData] = useState<UsageDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const loadRequestId = useRef(0)

  useEffect(() => {
    setInsightMode(initialMode)
  }, [initialMode])

  const selectInsightMode = useCallback((mode: InsightMode) => {
    setInsightMode(mode)
    const path = mode === 'kpi'
      ? '/admin/insights/service-desk-kpi'
      : mode === 'kpi-email'
        ? '/admin/insights/service-desk-kpi/email-delivery'
        : '/admin/insights/esip-usage'
    navigate(`${path}${mode === 'kpi' ? location.search : ''}`)
  }, [location.search, navigate])

  const load = useCallback(async () => {
    const requestId = loadRequestId.current + 1
    loadRequestId.current = requestId
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      params.set('range', 'custom')
      params.set('start_date', usageStartDate)
      params.set('end_date', usageEndDate)
      if (userEmail) params.set('user_email', userEmail)
      if (questionType) params.set('question_type', questionType)
      params.set('compare_previous', comparePrevious ? 'true' : 'false')
      params.set('group_by', groupBy)
      params.set('min_activity', minActivity)
      params.set('problem_signal', problemSignal)
      params.set('category_focus', categoryFocus)
      const result = await apiFetch<UsageDashboard>(`/api/admin/usage/dashboard?${params.toString()}`)
      if (requestId !== loadRequestId.current) return
      setData(result)
    } catch (e) {
      if (requestId !== loadRequestId.current) return
      setError((e as Error).message)
    } finally {
      if (requestId === loadRequestId.current) setLoading(false)
    }
  }, [usageStartDate, usageEndDate, userEmail, questionType, comparePrevious, groupBy, minActivity, problemSignal, categoryFocus])

  useEffect(() => { void load() }, [load])

  const updateUsageDateRange = useCallback((nextStartDate: string, nextEndDate: string) => {
    const params = new URLSearchParams(location.search)
    if (isDateInputValue(nextStartDate)) params.set('start_date', nextStartDate)
    else params.delete('start_date')
    if (isDateInputValue(nextEndDate)) params.set('end_date', nextEndDate)
    else params.delete('end_date')
    const nextSearch = params.toString()
    const nextUrl = nextSearch ? `${location.pathname}?${nextSearch}` : location.pathname
    if (`${location.pathname}${location.search}` !== nextUrl) {
      navigate(nextUrl, { replace: true })
    }
  }, [location.pathname, location.search, navigate])

  const setUsageRangeDays = useCallback((days: number) => {
    const end = isDateInputValue(usageEndDate) ? usageEndDate : defaultUsageEndDate
    updateUsageDateRange(addDaysToDateInput(end, -(days - 1)), end)
  }, [defaultUsageEndDate, updateUsageDateRange, usageEndDate])

  const usageRangeDays = useMemo(
    () => inclusiveDateRangeDays(usageStartDate, usageEndDate),
    [usageEndDate, usageStartDate],
  )

  const usageReportTitle = useMemo(() => {
    if (!isDateInputValue(usageStartDate) || !isDateInputValue(usageEndDate)) return 'ESIP Usage Report'
    if (usageStartDate === usageEndDate) return `ESIP Usage Report for ${formatReportDate(usageStartDate)}`
    return `ESIP Usage Report for ${formatReportDate(usageStartDate)} - ${formatReportDate(usageEndDate)}`
  }, [usageEndDate, usageStartDate])

  const downloadUsageExport = useCallback(async () => {
    setExporting(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      params.set('range', 'custom')
      params.set('start_date', usageStartDate)
      params.set('end_date', usageEndDate)
      if (userEmail) params.set('user_email', userEmail)
      if (questionType) params.set('question_type', questionType)
      params.set('compare_previous', comparePrevious ? 'true' : 'false')
      params.set('group_by', groupBy)
      params.set('min_activity', minActivity)
      params.set('problem_signal', problemSignal)
      params.set('category_focus', categoryFocus)
      params.set('format', 'xlsx')
      const res = await fetch(`/api/admin/usage/export?${params.toString()}`, {
        headers: await authHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`)
      }
      const blob = await res.blob()
      const fallbackRange = usageStartDate === usageEndDate ? usageStartDate : `${usageStartDate}_to_${usageEndDate}`
      const fallback = `esip-usage-${fallbackRange}.xlsx`
      const filename = downloadFilename(res.headers.get('Content-Disposition'), fallback)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExporting(false)
    }
  }, [
    categoryFocus,
    comparePrevious,
    groupBy,
    minActivity,
    problemSignal,
    questionType,
    usageEndDate,
    usageStartDate,
    userEmail,
  ])

  const topUsersChart = useMemo(
    () => (data?.users ?? [])
      .filter(user => user.queries_in_range > 0)
      .slice(0, 8)
      .map(user => ({
        name: shortPersonLabel(user),
        queries: user.queries_in_range,
      })),
    [data],
  )
  const questionTypeChart = useMemo(
    () => (data?.question_types ?? []).slice(0, 8).map(row => ({
      name: row.question_type.replace(' / ', '\n'),
      count: row.count,
      weak: row.weak_or_no_answer,
      noEvidence: row.no_source_count,
    })),
    [data],
  )
  const activityIntensityChart = useMemo(
    () => (data?.timeseries ?? []).map(row => ({
      date: row.date,
      active_users: row.active_users,
      queries_per_user: row.active_users > 0 ? Number((row.queries / row.active_users).toFixed(1)) : 0,
    })),
    [data],
  )
  const documentationOpportunities = data?.document_gaps.length ?? 0
  const selectedRangeIsToDate = data?.range.mode === 'to_date'
  const visibleUsers = data?.users.slice(0, 100) ?? []
  const tableAvgLatencyMs = useMemo(() => {
    if (!data) return null
    let weightedLatency = 0
    let weightedCount = 0
    for (const user of data.users) {
      if (user.avg_latency_ms === null || user.queries_in_range <= 0) continue
      weightedLatency += user.avg_latency_ms * user.queries_in_range
      weightedCount += user.queries_in_range
    }
    return weightedCount > 0 ? weightedLatency / weightedCount : null
  }, [data])

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-3 rounded-lg border bg-card p-3 shadow-sm lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold">Insights</h2>
            <div className="inline-flex rounded-lg border bg-muted/40 p-1">
              <button
                type="button"
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${insightMode === 'esip' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
                onClick={() => selectInsightMode('esip')}
              >
                ESIP Usage
              </button>
              <button
                type="button"
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${insightMode === 'kpi' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
                onClick={() => selectInsightMode('kpi')}
              >
                Service Desk KPI
              </button>
              <button
                type="button"
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${insightMode === 'kpi-email' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
                onClick={() => selectInsightMode('kpi-email')}
              >
                Email delivery
              </button>
            </div>
          </div>
        </div>
      </div>

      {insightMode === 'kpi' ? (
        <ServiceDeskKpiPanel />
      ) : insightMode === 'kpi-email' ? (
        <ServiceDeskKpiDeliveryCard />
      ) : (
        <>
          <div className="flex flex-col gap-3 rounded-lg border bg-card p-3 shadow-sm lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0">
              <h3 className="text-sm font-semibold">
                {usageReportTitle}
              </h3>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                From
                <Input
                  type="date"
                  className="h-9 w-[11rem]"
                  value={usageStartDate}
                  onChange={(e) => updateUsageDateRange(e.target.value, usageEndDate)}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                To
                <Input
                  type="date"
                  className="h-9 w-[11rem]"
                  value={usageEndDate}
                  onChange={(e) => updateUsageDateRange(usageStartDate, e.target.value)}
                />
              </label>
              <div className="flex h-9 items-center gap-1">
                {[1, 7, 30].map(days => {
                  const active = usageRangeDays === days
                  return (
                    <Button
                      key={days}
                      type="button"
                      variant={active ? 'default' : 'outline'}
                      size="sm"
                      className="focus-visible:ring-1 focus-visible:ring-ring/40"
                      aria-pressed={active}
                      onPointerUp={(event) => event.currentTarget.blur()}
                      onClick={() => setUsageRangeDays(days)}
                    >
                      {days} {days === 1 ? 'day' : 'days'}
                    </Button>
                  )
                })}
              </div>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                User
                <select
                  className="h-9 min-w-[13rem] rounded-md border bg-background px-3 text-sm text-foreground"
                  value={userEmail}
                  onChange={(e) => setUserEmail(e.target.value)}
                >
                  <option value="">All users</option>
                  {(data?.filters.users ?? []).map(user => (
                    <option key={user.email} value={user.email}>
                      {personLabel(user)} ({user.queries_to_date})
                    </option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Question type
                <select
                  className="h-9 min-w-[13rem] rounded-md border bg-background px-3 text-sm text-foreground"
                  value={questionType}
                  onChange={(e) => setQuestionType(e.target.value)}
                >
                  <option value="">All types</option>
                  {(data?.filters.question_types ?? []).map(type => (
                    <option key={type} value={type}>{type}</option>
                  ))}
                </select>
              </label>
              <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
                {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                Refresh
              </Button>
              <Button variant="default" size="sm" onClick={() => void downloadUsageExport()} disabled={!data || exporting}>
                {exporting ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
                Excel
              </Button>
            </div>
          </div>

      <div className="rounded-lg border bg-card p-4 shadow-sm">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold">Insights controls</h3>
              <InfoTip title="Insights controls">
                Use these controls to change the question you are asking the dashboard. Example: choose
                “No evidence shown” to see where ESIP answered without visible source links, ticket cards, or analytics results.
              </InfoTip>
            </div>
            <p className="text-xs text-muted-foreground">
              Adjust the analysis depth without changing the raw log access boundary.
            </p>
          </div>
          <label className="flex h-9 items-center gap-2 rounded-md border bg-background px-3 text-sm text-muted-foreground">
            <input
              type="checkbox"
              className="size-4"
              checked={comparePrevious}
              onChange={(e) => setComparePrevious(e.target.checked)}
            />
            Compare previous period
          </label>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Group by
            <select
              className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value)}
            >
              <option value="user">User</option>
              <option value="question_type">Question type</option>
              <option value="day">Day</option>
              <option value="week">Week</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Minimum activity
            <select
              className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"
              value={minActivity}
              onChange={(e) => setMinActivity(e.target.value)}
            >
              <option value="0">Show all users</option>
              <option value="3">3+ queries in range</option>
              <option value="10">10+ queries in range</option>
              <option value="25">25+ queries in range</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Problem signals
            <select
              className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"
              value={problemSignal}
              onChange={(e) => setProblemSignal(e.target.value)}
            >
              <option value="all">All questions</option>
              <option value="no_sources">No evidence shown</option>
              {isSuperAdmin && <option value="weak_answers">Weak answers</option>}
              <option value="high_latency">High latency</option>
              <option value="repeated_topic">Repeated topic</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Category focus
            <select
              className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"
              value={categoryFocus}
              onChange={(e) => setCategoryFocus(e.target.value)}
            >
              <option value="all">All categories</option>
              <option value="knowledge_documents">Knowledge / documents</option>
              <option value="exclude_summaries">Exclude summaries</option>
              <option value="exclude_ticket_lookups">Exclude ticket lookups</option>
            </select>
          </label>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && !data ? (
        <div className="flex min-h-[20rem] items-center justify-center rounded-lg border bg-card">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : data && (
        <>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
            <UsageMetricCard
              title="Queries to date"
              value={data.summary.total_queries_to_date.toLocaleString()}
              detail={data.controls.problem_signal === 'all' && data.controls.category_focus === 'all' ? 'All users' : 'All-time adoption baseline'}
              icon={<Activity className="size-4" />}
              info="Total real questions asked in ESIP so far. Language-switch translation clicks are not counted. Example: one analyst asks three different questions, that counts as 3."
            />
            <UsageMetricCard
              title="Selected range"
              value={data.summary.total_queries_in_range.toLocaleString()}
              detail={data.range.mode === 'to_date' ? 'To date' : fmtDelta(data.summary.total_queries_in_range, data.comparison?.total_queries_in_range)}
              icon={<MessageSquareText className="size-4" />}
              info="Questions that match the current filters and date range. Example: if Range is Last 7 days, this shows only questions from that period."
            />
            <UsageMetricCard
              title="Active users"
              value={data.summary.active_users_in_range.toLocaleString()}
              detail={(
                <MetricDetail
                  primary={`${data.summary.avg_queries_per_active_user} avg queries/user`}
                  comparison={data.comparison ? fmtDelta(data.summary.active_users_in_range, data.comparison.active_users_in_range) : undefined}
                />
              )}
              icon={<UsersRound className="size-4" />}
              info="How many different people used ESIP in the selected range. Example: 5 active users means 5 people asked at least one question."
            />
            <UsageMetricCard
              title="Top category"
              value={<span className="text-base leading-tight">{data.summary.top_question_type ?? '—'}</span>}
              detail="Most common question type"
              icon={<FileSearch className="size-4" />}
              info="The most common kind of question in the selected data. Example: “Document lookup” means users mostly asked ESIP to find docs or articles."
            />
            <UsageMetricCard
              title="No evidence shown"
              value={data.summary.no_source_turns.toLocaleString()}
              detail={(
                <MetricDetail
                  primary={`${data.source_coverage.evidence_coverage_pct}% with evidence`}
                  comparison={data.comparison ? fmtDelta(data.summary.no_source_turns, data.comparison.no_source_turns) : undefined}
                />
              )}
              icon={<Database className="size-4" />}
              info="Answers where ESIP did not show visible evidence artifacts: source links, related ticket cards, or analytics results. Example: this can mean we need better retrieval or clearer citations."
            />
            {isSuperAdmin ? (
              <UsageMetricCard
                title="Weak answers"
                value={data.summary.weak_or_no_answer_turns.toLocaleString()}
                detail={data.comparison ? fmtDelta(data.summary.weak_or_no_answer_turns, data.comparison.weak_or_no_answer_turns) : 'Technical quality signal'}
                icon={<AlertTriangle className="size-4" />}
                info="Technical signal for Super Admins only. It means ESIP said it could not find enough information. Example: useful for debugging search or missing documents."
              />
            ) : (
              <UsageMetricCard
                title="Documentation opportunities"
                value={documentationOpportunities.toLocaleString()}
                detail="Topics where sources or confidence can improve"
                icon={<FileSearch className="size-4" />}
                info="Topics that may need better documentation or source coverage. Example: repeated questions with no supporting source can become a candidate for a new article or Drive document."
              />
            )}
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold">Queries over time</h3>
                    <InfoTip title="Queries over time">
                      Shows daily question volume only. Example: a tall bar on Monday means users asked many
                      questions that day.
                    </InfoTip>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Daily question volume.
                    {fmtDateShort(data.range.start_at)} to {fmtDateShort(data.range.end_at)}
                  </p>
                </div>
              </div>
              <div className="h-[260px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.timeseries}>
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Legend />
                    <Bar dataKey="queries" name="Queries" fill="#2563eb" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">Active users and intensity</h3>
                <InfoTip title="Active users and intensity">
                  Shows how many people used ESIP and how heavily they used it. Example: 4 active users and
                  10 queries per user means a small group used ESIP heavily.
                </InfoTip>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">
                Daily unique users plus average queries per active user.
              </p>
              <div className="h-[260px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={activityIntensityChart}>
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                    <YAxis allowDecimals tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Legend />
                    <Bar dataKey="active_users" name="Active users" fill="#16a34a" radius={[3, 3, 0, 0]} />
                    <Bar dataKey="queries_per_user" name="Queries per user" fill="#0f766e" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">Top users in selected range</h3>
                <InfoTip title="Top users in selected range">
                  Shows who asked the most questions in the current filters. Example: if one analyst has 50
                  queries, they are one of the main users of ESIP.
                </InfoTip>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">Direct answer to query count by user.</p>
              <div className="h-[280px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={topUsersChart} layout="vertical" margin={{ left: 18, right: 12 }} barCategoryGap={10}>
                    <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                    <YAxis type="category" dataKey="name" width={110} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="queries" name="Queries" fill="#0f766e" radius={[0, 3, 3, 0]} barSize={22} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">Question types</h3>
                <InfoTip title="Question types">
                  Groups questions by what the user was trying to do. Example: “Document lookup” means users
                  asked ESIP to find a document or article.
                </InfoTip>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">What users are asking ESIP to do.</p>
              <div className="h-[280px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={questionTypeChart}>
                    <XAxis dataKey="name" tick={{ fontSize: 10 }} interval={0} height={58} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Legend />
                    <Bar dataKey="count" name="Queries" fill="#2563eb" radius={[3, 3, 0, 0]} />
                    {isSuperAdmin && <Bar dataKey="weak" name="Weak" fill="#f97316" radius={[3, 3, 0, 0]} />}
                    <Bar dataKey="noEvidence" name="No evidence" fill="#dc2626" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>

          <div className="rounded-lg border bg-card p-4 shadow-sm">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">Documentation opportunities</h3>
                <InfoTip title="Documentation opportunities">
                  Highlights topics where ESIP may need better source material. Example: repeated “No evidence”
                  questions can point to a missing or hard-to-find document.
                </InfoTip>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">
                Repeated topics where ESIP needs better source coverage or clearer documentation.
              </p>
              <div className="grid gap-3 lg:grid-cols-2">
                {data.document_gaps.length === 0 ? (
                  <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground lg:col-span-2">
                    No document-gap signals in this range.
                  </div>
                ) : data.document_gaps.slice(0, 8).map((gap) => (
                  <div key={`${gap.question_type}-${gap.theme}`} className="rounded-md border bg-muted/20 p-4">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="text-sm font-semibold">{gap.theme}</p>
                        <p className="text-xs text-muted-foreground">{gap.question_type}</p>
                      </div>
                      <div className="flex flex-wrap gap-2 text-xs tabular-nums text-muted-foreground">
                        <span className="rounded border bg-background px-2 py-0.5">{gap.count} queries</span>
                        <span className="rounded border bg-background px-2 py-0.5">{gap.users_affected} users</span>
                        <span className="rounded border bg-background px-2 py-0.5">{gap.no_source_count} no source</span>
                      </div>
                    </div>
                    <div className="mt-3 space-y-1.5 text-xs">
                      <p className="font-medium text-muted-foreground">Suggested next step</p>
                      <p className="font-medium text-foreground">{gap.suggested_action}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="border-b p-4">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">Queries by user</h3>
                <InfoTip title="Queries by user">
                  Shows how many questions each user has asked. Example: this directly answers “count of
                  queries by user to date.”
                </InfoTip>
              </div>
              <p className="text-xs text-muted-foreground">
                Adoption by person: total volume, selected-range share, activity, use case, and evidence signal.
                {data.users_unfiltered_count > data.users.length
                  ? ` Hidden by activity threshold: ${data.users_unfiltered_count - data.users.length}.`
                  : ''}
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[920px] text-sm">
                <thead className="border-b bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium">User</th>
                    <th className="px-4 py-2 text-right font-medium">Usage</th>
                    <th className="px-4 py-2 text-right font-medium">Share</th>
                    <th className="px-4 py-2 text-left font-medium">Activity</th>
                    <th className="px-4 py-2 text-left font-medium">Top use case</th>
                    <th className="px-4 py-2 text-left font-medium">Evidence / gaps</th>
                    <th className="px-4 py-2 text-right font-medium">Avg latency</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleUsers.map(user => {
                    const share = data.summary.total_queries_in_range > 0
                      ? Math.round((user.queries_in_range / data.summary.total_queries_in_range) * 100)
                      : 0
                    return (
                    <tr key={user.email ?? personLabel(user)} className="border-b last:border-b-0">
                      <td className="px-4 py-2 align-middle">
                        <div className="font-medium">{personLabel(user)}</div>
                        {user.display_name && (
                          <div className="text-xs text-muted-foreground">{user.email ?? 'Unknown email'}</div>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">
                        <div className="font-semibold">{user.queries_to_date.toLocaleString()} total</div>
                        {!selectedRangeIsToDate && (
                          <div className="text-xs text-muted-foreground">{user.queries_in_range.toLocaleString()} range</div>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right align-middle font-medium tabular-nums">
                        {share}%
                      </td>
                      <td className="px-4 py-2 align-middle">
                        <div className="text-muted-foreground">Last: {fmtDateTimeShort(user.last_active_at)}</div>
                        <div className="text-xs text-muted-foreground">
                          {user.active_days} active {user.active_days === 1 ? 'day' : 'days'} · first {fmtDateShort(user.first_seen_at)}
                        </div>
                      </td>
                      <td className="px-4 py-2 align-middle text-muted-foreground">{shortQuestionType(user.top_question_type)}</td>
                      <td className="px-4 py-2 align-middle">
                        <div className="font-medium tabular-nums">{user.evidence_rate_pct}% evidence shown</div>
                        <div className="text-xs text-muted-foreground">
                          {user.documentation_opportunities > 0
                            ? `${user.documentation_opportunities} doc gap${user.documentation_opportunities === 1 ? '' : 's'}`
                            : `${user.no_source_count} no evidence`}
                        </div>
                      </td>
                      <td className="px-4 py-2 text-right align-middle tabular-nums">
                        {user.avg_latency_ms !== null ? `${Math.round(user.avg_latency_ms / 1000)}s` : '—'}
                      </td>
                    </tr>
                    )
                  })}
                </tbody>
                <tfoot className="border-t bg-muted/30 text-sm">
                  <tr>
                    <td className="px-4 py-3 font-semibold">
                      Total
                      <div className="text-xs font-normal text-muted-foreground">
                        {data.summary.active_users_in_range.toLocaleString()} active users in range
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right font-semibold tabular-nums">
                      <div>{data.summary.total_queries_to_date.toLocaleString()} total</div>
                      {!selectedRangeIsToDate && (
                        <div className="text-xs font-normal text-muted-foreground">
                          {data.summary.total_queries_in_range.toLocaleString()} range
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right font-semibold tabular-nums">100%</td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {data.summary.avg_queries_per_active_user} avg queries/user
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {shortQuestionType(data.summary.top_question_type)}
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium tabular-nums">{data.source_coverage.evidence_coverage_pct}% evidence shown</div>
                      <div className="text-xs text-muted-foreground">{documentationOpportunities} doc opportunities</div>
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {tableAvgLatencyMs !== null ? `${Math.round(tableAvgLatencyMs / 1000)}s` : '—'}
                    </td>
                  </tr>
                </tfoot>
          </table>
            </div>
          </div>
        </>
      )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Impact tab + printable report
// ---------------------------------------------------------------------------

type ImpactPeriodKey = 'baseline' | 'ramp' | 'post'

interface ImpactDashboard {
  range: Record<ImpactPeriodKey, { label: string; start_date: string; end_date: string }> & {
    rollout_date: string
    as_of_date?: string
    post_started?: boolean
    current_phase?: ImpactPeriodKey
    display_period?: ImpactPeriodKey
    ramp_day?: number
  }
  summary: {
    display_period?: ImpactPeriodKey
    display_period_label?: string
    post_started?: boolean
    ramp_day?: number
    active_users_post: number
    active_users_display?: number
    repeat_users_post: number
    esip_assisted_tickets_post: number
    esip_assisted_tickets_display?: number
    median_reply_latency_post_hours: number | null
    median_reply_latency_display_hours?: number | null
    solved_tickets_post: number
    solved_tickets_display?: number
    outstanding_tickets: number
    cautious_label: string
  }
  adoption: {
    periods: Record<ImpactPeriodKey, { label: string; events: number; active_users: number; assisted_tickets: number }>
    workflows: Array<{ period: ImpactPeriodKey; workflow: string; events: number; users: number; tickets: number }>
    timeline: Array<{ date: string; events: number; active_users: number; assisted_tickets: number }>
    repeat_users_post: number
  }
  response_speed: {
    periods: Record<ImpactPeriodKey, {
      replies: number
      assisted_replies: number
      median_latency_hours: number | null
      assisted_median_latency_hours: number | null
      non_assisted_median_latency_hours: number | null
    }> & { headline_delta: { value: number | null; direction: string; label: string } }
  }
  first_action: {
    periods: Record<ImpactPeriodKey, { tickets: number; median_first_action_hours: number | null }>
  }
  outcomes: {
    periods: Record<ImpactPeriodKey, {
      solved_tickets: number
      median_resolution_hours: number | null
      sla_breach_rate_pct: number
      sla_known_tickets: number
      single_cycle_solve_rate_pct: number
    }>
    headline_solved_delta: { value: number | null; direction: string; label: string }
    headline_resolution_delta: { value: number | null; direction: string; label: string }
    outstanding_tickets: number
    assignee_email_coverage: boolean
  }
  quality: {
    periods: Record<ImpactPeriodKey, {
      chat_turns: number
      weak_or_no_answer: number
      no_evidence: number
      documentation_gap_signals: number
      answer_feedback_events: number
      answer_copied_events: number
      not_useful_events: number
    }>
  }
  users: Array<{
    key: string
    email: string | null
    display_name: string
    access_granted_at: string
    confidence: string
    confidence_reason: string
    identity_source: string
    usage: Record<ImpactPeriodKey, { events: number; active_days: number; assisted_tickets: number }>
    replies: Record<ImpactPeriodKey, { replies: number; assisted_replies: number; median_latency_hours: number | null }>
    solved: Record<ImpactPeriodKey, {
      solved_tickets: number
      median_resolution_hours: number | null
      sla_breach_rate_pct: number
      single_cycle_solve_rate_pct: number
    }>
    outstanding_tickets: number
    reply_latency_delta: { value: number | null; direction: string; label: string }
    solved_delta: { value: number | null; direction: string; label: string }
  }>
  insights: {
    wins: string[]
    concerns: string[]
    caveats: string[]
  }
  warnings: string[]
  report?: {
    title: string
    subtitle: string
    headline: string
    generated_at: string
    kpis: Array<{ label: string; value: number | string | null; suffix?: string; detail: string }>
  }
}

type ImpactUserRow = ImpactDashboard['users'][number]

type ImpactEngineerSortField =
  | 'engineer'
  | 'confidence'
  | 'esip_touches'
  | 'assisted_tickets'
  | 'reply_speed'
  | 'solved'
  | 'outstanding'

type ImpactEngineerSort = {
  field: ImpactEngineerSortField
  direction: 'asc' | 'desc'
} | null

const IMPACT_SORT_LABELS: Record<ImpactEngineerSortField, string> = {
  engineer: 'Engineer',
  confidence: 'Confidence',
  esip_touches: 'ESIP touches',
  assisted_tickets: 'Assisted tickets',
  reply_speed: 'Reply speed',
  solved: 'Solved',
  outstanding: 'Outstanding',
}

const IMPACT_CONFIDENCE_RANK: Record<string, number> = {
  'Enough data': 0,
  'Ramp-up': 1,
  'Low ESIP usage': 2,
}

function defaultImpactSortDirection(field: ImpactEngineerSortField): 'asc' | 'desc' {
  return field === 'engineer' || field === 'confidence' || field === 'reply_speed' ? 'asc' : 'desc'
}

function impactSortNumber(
  user: ImpactUserRow,
  field: ImpactEngineerSortField,
  displayPeriod: ImpactPeriodKey,
): number | undefined {
  switch (field) {
    case 'confidence':
      return IMPACT_CONFIDENCE_RANK[user.confidence] ?? 99
    case 'esip_touches':
      return user.usage[displayPeriod].events
    case 'assisted_tickets':
      return user.usage[displayPeriod].assisted_tickets
    case 'reply_speed':
      return user.replies[displayPeriod].median_latency_hours ?? undefined
    case 'solved':
      return user.solved[displayPeriod].solved_tickets
    case 'outstanding':
      return user.outstanding_tickets
    default:
      return undefined
  }
}

function compareImpactUsers(
  left: ImpactUserRow,
  right: ImpactUserRow,
  sort: ImpactEngineerSort,
  displayPeriod: ImpactPeriodKey,
): number {
  if (!sort) return 0
  const direction = sort.direction === 'asc' ? 1 : -1
  if (sort.field === 'engineer') {
    const result = personLabel(left).localeCompare(personLabel(right), undefined, { sensitivity: 'base' })
    return result * direction
  }
  const leftValue = impactSortNumber(left, sort.field, displayPeriod)
  const rightValue = impactSortNumber(right, sort.field, displayPeriod)
  if (leftValue === undefined && rightValue === undefined) return 0
  if (leftValue === undefined) return 1
  if (rightValue === undefined) return -1
  if (leftValue === rightValue) {
    return personLabel(left).localeCompare(personLabel(right), undefined, { sensitivity: 'base' })
  }
  return (leftValue - rightValue) * direction
}

function fmtHours(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value <= 0) return '<1m'
  if (value < 1) {
    const minutes = Math.round(value * 60)
    return minutes <= 0 ? '<1m' : `${minutes}m`
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)}h`
}

function confidenceClass(label: string): string {
  if (label === 'Enough data') return 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200'
  if (label === 'Ramp-up') return 'border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-100'
  if (label === 'Low ESIP usage') return 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200'
  return 'border-muted bg-muted/40 text-muted-foreground'
}

function ImpactMetricCard({
  title, value, detail, icon,
}: { title: string; value: ReactNode; detail: ReactNode; icon: ReactNode }) {
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-normal text-muted-foreground">{title}</p>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground">{value}</div>
          <div className="mt-1 text-xs leading-relaxed text-muted-foreground">{detail}</div>
        </div>
        <div className="rounded-md border bg-muted/40 p-2 text-muted-foreground">{icon}</div>
      </div>
    </div>
  )
}

function ImpactDashboardView({ data, reportMode = false }: { data: ImpactDashboard; reportMode?: boolean }) {
  const [impactEngineerSort, setImpactEngineerSort] = useState<ImpactEngineerSort>(null)
  const displayPeriod = data.summary.display_period ?? data.range.display_period ?? 'post'
  const postStarted = Boolean(data.summary.post_started ?? data.range.post_started ?? displayPeriod === 'post')
  const periodLabel = data.summary.display_period_label ?? data.range[displayPeriod].label
  const rampDay = data.summary.ramp_day ?? data.range.ramp_day ?? 0
  const activeUsers = data.summary.active_users_display ?? data.adoption.periods[displayPeriod].active_users
  const assistedTickets = data.summary.esip_assisted_tickets_display ?? data.adoption.periods[displayPeriod].assisted_tickets
  const replySpeed = data.summary.median_reply_latency_display_hours ?? data.response_speed.periods[displayPeriod].median_latency_hours
  const solvedTickets = data.summary.solved_tickets_display ?? data.outcomes.periods[displayPeriod].solved_tickets
  const periodChart = (['baseline', 'ramp', 'post'] as ImpactPeriodKey[]).map(period => ({
    period: data.range[period].label,
    usage: data.adoption.periods[period].events,
    assisted: data.adoption.periods[period].assisted_tickets,
    replies: data.response_speed.periods[period].replies,
    solved: data.outcomes.periods[period].solved_tickets,
  }))
  const workflowChart = data.adoption.workflows
    .filter(row => row.period === displayPeriod)
    .slice(0, 6)
    .map(row => ({ workflow: row.workflow, events: row.events }))
  const periodDetail = postStarted
    ? 'Stable post-ramp window'
    : `Ramp-up day ${rampDay || 1} of 7. Not an impact conclusion`
  const visibleImpactUsers = useMemo(() => {
    const rows = data.users.slice(0, 80)
    if (!impactEngineerSort) return rows
    return [...rows].sort((left, right) => compareImpactUsers(left, right, impactEngineerSort, displayPeriod))
  }, [data.users, displayPeriod, impactEngineerSort])
  const updateImpactEngineerSort = (field: ImpactEngineerSortField) => {
    setImpactEngineerSort(current => {
      if (current?.field === field) {
        return { field, direction: current.direction === 'asc' ? 'desc' : 'asc' }
      }
      return { field, direction: defaultImpactSortDirection(field) }
    })
  }
  const renderImpactSortIcon = (field: ImpactEngineerSortField) => {
    const active = impactEngineerSort?.field === field
    if (!active) return <ChevronDown className="size-3 opacity-25" />
    return impactEngineerSort.direction === 'asc'
      ? <ChevronUp className="size-3" />
      : <ChevronDown className="size-3" />
  }
  const impactHeaderButton = (
    field: ImpactEngineerSortField,
    label: ReactNode,
    align: 'left' | 'right' = 'right',
  ) => {
    const active = impactEngineerSort?.field === field
    return (
      <button
        type="button"
        className={`inline-flex w-full items-center gap-1 rounded px-1 py-0.5 text-xs font-medium hover:bg-background/70 focus:outline-none focus:ring-2 focus:ring-slate-500 ${align === 'left' ? 'justify-start' : 'justify-end'} ${active ? 'text-foreground' : 'text-muted-foreground'}`}
        onClick={() => updateImpactEngineerSort(field)}
        title={`Sort by ${IMPACT_SORT_LABELS[field]}`}
      >
        <span>{label}</span>
        {renderImpactSortIcon(field)}
      </button>
    )
  }
  const impactSortDescription = impactEngineerSort
    ? `Sorted by ${IMPACT_SORT_LABELS[impactEngineerSort.field]}, ${impactEngineerSort.direction === 'asc' ? 'ascending' : 'descending'}.`
    : 'Default order from the impact report.'

  return (
    <div className={reportMode ? 'space-y-5' : 'space-y-4'}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <ImpactMetricCard
          title={postStarted ? 'Active users' : 'Ramp-up users'}
          value={activeUsers.toLocaleString()}
          detail={postStarted ? `${data.summary.repeat_users_post.toLocaleString()} repeat users after ramp` : periodDetail}
          icon={<UsersRound className="size-4" />}
        />
        <ImpactMetricCard
          title="ESIP-assisted tickets"
          value={assistedTickets.toLocaleString()}
          detail={postStarted ? 'Tickets with a ESIP touch after ramp-up' : 'Ramp-up tickets with a ESIP touch'}
          icon={<Activity className="size-4" />}
        />
        <ImpactMetricCard
          title="Reply speed"
          value={fmtHours(replySpeed)}
          detail={postStarted ? data.response_speed.periods.headline_delta.label : 'Shown for ramp-up only. Comparison waits until day 8'}
          icon={<Clock3 className="size-4" />}
        />
        <ImpactMetricCard
          title={postStarted ? 'Solved tickets' : 'Solved during ramp-up'}
          value={solvedTickets.toLocaleString()}
          detail={`${data.summary.outstanding_tickets.toLocaleString()} currently outstanding`}
          icon={<CheckCircle2 className="size-4" />}
        />
      </div>

      {!postStarted && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 shadow-sm dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
          ESIP is still in the first-week learning period. The numbers above show {periodLabel.toLowerCase()} activity so the page is useful today. Headline impact conclusions still wait for stable post-ramp data.
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-lg border bg-card p-4 shadow-sm">
          <h3 className="text-sm font-semibold">Before, ramp-up, after</h3>
          <p className="mb-3 text-xs text-muted-foreground">
            Ramp-up is shown separately and excluded from headline conclusions.
          </p>
          <div className="h-[250px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={periodChart}>
                <XAxis dataKey="period" tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend />
                <Bar dataKey="usage" name="ESIP touches" fill="#2563eb" radius={[3, 3, 0, 0]} />
                <Bar dataKey="assisted" name="Assisted tickets" fill="#0f766e" radius={[3, 3, 0, 0]} />
                <Bar dataKey="solved" name="Solved" fill="#16a34a" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="rounded-lg border bg-card p-4 shadow-sm">
          <h3 className="text-sm font-semibold">Where ESIP was used</h3>
          <p className="mb-3 text-xs text-muted-foreground">
            {postStarted ? 'Top post-ramp workflows.' : 'Top ramp-up workflows so far.'}
          </p>
          <div className="h-[250px]">
            {workflowChart.length === 0 ? (
              <div className="flex h-full items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
                {postStarted ? 'Waiting for post-ramp usage.' : 'Waiting for ramp-up usage.'}
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={workflowChart} layout="vertical" margin={{ left: 24, right: 12 }}>
                  <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                  <YAxis type="category" dataKey="workflow" width={110} tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Bar dataKey="events" name="Events" fill="#0f766e" radius={[0, 3, 3, 0]} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-lg border bg-card p-4 shadow-sm">
          <h3 className="text-sm font-semibold">What improved</h3>
          <div className="mt-3 space-y-2 text-sm">
            {data.insights.wins.length === 0 ? (
              <p className="text-muted-foreground">No stable post-ramp wins yet. Keep collecting data.</p>
            ) : data.insights.wins.map(item => (
              <div key={item} className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-100">
                {item}
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-lg border bg-card p-4 shadow-sm">
          <h3 className="text-sm font-semibold">What still needs work</h3>
          <div className="mt-3 space-y-2 text-sm">
            {data.insights.concerns.length === 0 ? (
              <p className="text-muted-foreground">No major concerns in this range.</p>
            ) : data.insights.concerns.map(item => (
              <div key={item} className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
                {item}
              </div>
            ))}
          </div>
        </div>
      </div>

      {!reportMode && (
        <div className="rounded-lg border bg-card shadow-sm">
          <div className="border-b p-4">
            <h3 className="text-sm font-semibold">Engineer view</h3>
            <p className="text-xs text-muted-foreground">
              Names are shown for coaching and product analysis. Confidence labels protect against low-sample conclusions.
              {!postStarted && ' Showing ramp-up activity because stable post-ramp has not started.'}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">{impactSortDescription}</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-sm">
              <thead className="border-b bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">{impactHeaderButton('engineer', 'Engineer', 'left')}</th>
                  <th className="px-4 py-2 text-left font-medium">{impactHeaderButton('confidence', 'Confidence', 'left')}</th>
                  <th className="px-4 py-2 text-right font-medium">{impactHeaderButton('esip_touches', 'ESIP touches')}</th>
                  <th className="px-4 py-2 text-right font-medium">{impactHeaderButton('assisted_tickets', 'Assisted tickets')}</th>
                  <th className="px-4 py-2 text-right font-medium">{impactHeaderButton('reply_speed', 'Reply speed')}</th>
                  <th className="px-4 py-2 text-right font-medium">{impactHeaderButton('solved', 'Solved')}</th>
                  <th className="px-4 py-2 text-right font-medium">{impactHeaderButton('outstanding', 'Outstanding')}</th>
                </tr>
              </thead>
              <tbody>
                {visibleImpactUsers.map(user => (
                  <tr key={user.key} className="border-b last:border-b-0">
                    <td className="px-4 py-2">
                      <div className="font-medium">{personLabel(user)}</div>
                      {user.display_name && (
                        <div className="text-xs text-muted-foreground">{user.email ?? user.key}</div>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <span className={`inline-flex rounded-md border px-2 py-1 text-xs font-medium ${confidenceClass(user.confidence)}`}>
                        {user.confidence}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{user.usage[displayPeriod].events.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{user.usage[displayPeriod].assisted_tickets.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      <div>{fmtHours(user.replies[displayPeriod].median_latency_hours)}</div>
                      <div className="text-xs text-muted-foreground">{postStarted ? user.reply_latency_delta.label : 'Ramp-up only'}</div>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      <div>{user.solved[displayPeriod].solved_tickets.toLocaleString()}</div>
                      <div className="text-xs text-muted-foreground">{postStarted ? user.solved_delta.label : 'Ramp-up only'}</div>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{user.outstanding_tickets.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="rounded-lg border bg-card p-4 text-xs leading-relaxed text-muted-foreground shadow-sm">
        <div className="font-semibold text-foreground">Data notes</div>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          {data.insights.caveats.map(item => <li key={item}>{item}</li>)}
          {data.warnings.map(item => <li key={item}>{item}</li>)}
        </ul>
      </div>
    </div>
  )
}

function ImpactTab() {
  const [rolloutDate, setRolloutDate] = useState('2026-05-26')
  const [data, setData] = useState<ImpactDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [exporting, setExporting] = useState<'pdf' | 'xlsx' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({ rollout_date: rolloutDate })
      const result = await apiFetch<ImpactDashboard>(`/api/admin/impact/dashboard?${params.toString()}`)
      setData(result)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [rolloutDate])

  useEffect(() => { void load() }, [load])

  const downloadImpactExport = useCallback(async (format: 'pdf' | 'xlsx') => {
    setExporting(format)
    setError(null)
    try {
      const params = new URLSearchParams({ rollout_date: rolloutDate, format })
      const res = await fetch(`/api/admin/impact/export?${params.toString()}`, {
        headers: await authHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`)
      }
      const blob = await res.blob()
      const fallback = `esip-impact-${rolloutDate}.${format === 'xlsx' ? 'xlsx' : 'pdf'}`
      const filename = downloadFilename(res.headers.get('Content-Disposition'), fallback)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExporting(null)
    }
  }, [rolloutDate])

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 rounded-lg border bg-card p-4 shadow-sm lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">Impact</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Cautious before/ramp/after view of ESIP usage, response speed, solved work, and quality guardrails.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Rollout date
            <Input className="h-9 w-40" type="date" value={rolloutDate} onChange={e => setRolloutDate(e.target.value)} />
          </label>
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            Refresh
          </Button>
          <Button size="sm" onClick={() => void downloadImpactExport('pdf')} disabled={!data || Boolean(exporting)}>
            {exporting === 'pdf' ? <Loader2 className="size-4 animate-spin" /> : <Printer className="size-4" />}
            Report PDF
          </Button>
          <Button variant="outline" size="sm" onClick={() => void downloadImpactExport('xlsx')} disabled={!data || Boolean(exporting)}>
            {exporting === 'xlsx' ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
            Excel
          </Button>
        </div>
      </div>
      {error && <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
      {loading && !data ? (
        <div className="flex min-h-[20rem] items-center justify-center rounded-lg border bg-card">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : data ? (
        <ImpactDashboardView data={data} />
      ) : null}
    </div>
  )
}

export function AdminImpactReportPage() {
  const searchParams = useMemo(() => new URLSearchParams(window.location.search), [])
  const rolloutDate = searchParams.get('rollout_date') ?? '2026-05-26'
  const [data, setData] = useState<ImpactDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const params = new URLSearchParams({ rollout_date: rolloutDate })
    apiFetch<ImpactDashboard>(`/api/admin/impact/report?${params.toString()}`)
      .then(setData)
      .catch(e => setError((e as Error).message))
      .finally(() => setLoading(false))
  }, [rolloutDate])

  if (loading) {
    return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>
  }
  if (error) {
    return <div className="m-6 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">{error}</div>
  }
  if (!data) return null

  return (
    <PageScroll>
      <div className="mx-auto max-w-5xl space-y-5 p-6 print:p-0">
        <div className="flex items-start justify-between gap-4 print:hidden">
          <Button variant="outline" size="sm" asChild><Link to="/admin/panel">Back</Link></Button>
          <Button size="sm" onClick={() => window.print()}><Printer className="size-4" />Print / Save PDF</Button>
        </div>
        <div className="rounded-lg border bg-card p-6 shadow-sm print:border-0 print:shadow-none">
          <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
            <div>
              <p className="text-xs font-medium uppercase tracking-normal text-muted-foreground">{data.report?.subtitle}</p>
              <h1 className="mt-1 text-2xl font-semibold">{data.report?.title}</h1>
              <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">{data.report?.headline}</p>
            </div>
            <div className="text-right text-xs text-muted-foreground">
              <div>Rollout: {data.range.rollout_date}</div>
              <div>Generated: {data.report?.generated_at ? new Date(data.report.generated_at).toLocaleString() : '—'}</div>
            </div>
          </div>
        </div>
        <ImpactDashboardView data={data} reportMode />
      </div>
    </PageScroll>
  )
}

// ---------------------------------------------------------------------------
// Quality telemetry tab (Super Admin only)
// ---------------------------------------------------------------------------

function qualityLabelClass(value: string | null | undefined): string {
  const safe = (value || '').toLowerCase()
  if (safe === 'good') return 'border-emerald-200 bg-emerald-50 text-emerald-800'
  if (safe === 'weak' || safe === 'suspect') return 'border-amber-200 bg-amber-50 text-amber-800'
  if (safe === 'likely_wrong' || safe === 'unsafe') return 'border-red-200 bg-red-50 text-red-800'
  if (safe === 'insufficient_evidence') return 'border-sky-200 bg-sky-50 text-sky-800'
  return 'border-muted bg-muted/60 text-muted-foreground'
}

function qualityPct(part: number, total: number): string {
  if (total <= 0) return '0%'
  return `${Math.round((part / total) * 100)}%`
}

function bucketCount(items: QualityTelemetryBucket[], names: string[]): number {
  const selected = new Set(names)
  return items.reduce((total, item) => total + (selected.has(item.name) ? item.count : 0), 0)
}

function fmtCad(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return new Intl.NumberFormat('en-CA', {
    style: 'currency',
    currency: 'CAD',
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value)
}

function qualityLimitFromParam(value: string | null): number {
  const parsed = Number(value ?? 25)
  return [10, 25, 50, 100].includes(parsed) ? parsed : 25
}

function dispatcherDashboardPath(startDate: string, endDate: string): string {
  const params = new URLSearchParams({ start_date: startDate, end_date: endDate, limit: '50' })
  return `/api/admin/ai-dispatcher/dashboard?${params.toString()}`
}

function chatLogsLink(traceId?: string | null, turnId?: string | null): string {
  const params = new URLSearchParams()
  if (traceId) params.set('trace_id', traceId)
  if (turnId) params.set('turn_id', turnId)
  const query = params.toString()
  const hash = turnId ? `#chat-log-${encodeURIComponent(turnId)}` : ''
  return `/admin/chat-logs${query ? `?${query}` : ''}${hash}`
}

function ModelTransitionCapturePanel() {
  const [config, setConfig] = useState<ModelTransitionCaptureConfig | null>(null)
  const [draft, setDraft] = useState<ModelTransitionCaptureConfig | null>(null)
  const [newEmail, setNewEmail] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await apiFetch<ModelTransitionCaptureConfig>('/api/admin/model-transition-capture/config')
      setConfig(next)
      setDraft(next)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  function addEmail() {
    const email = newEmail.trim().toLowerCase()
    if (!draft || !email) return
    if (!email.endsWith(`@${REFERENCE_TENANT_DOMAIN}`)) {
      setError(`Use a ${REFERENCE_TENANT_DOMAIN} email.`)
      return
    }
    if (draft.allowed_emails.includes(email)) {
      setNewEmail('')
      return
    }
    if (draft.allowed_emails.length >= draft.max_allowed_emails) {
      setError(`This temporary scope is limited to ${draft.max_allowed_emails} users.`)
      return
    }
    setDraft({ ...draft, allowed_emails: [...draft.allowed_emails, email] })
    setNewEmail('')
    setSaved(false)
    setError(null)
  }

  async function save() {
    if (!draft) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const next = await apiFetch<ModelTransitionCaptureConfig>('/api/admin/model-transition-capture/config', {
        method: 'PUT',
        body: JSON.stringify({ enabled: draft.enabled, allowed_emails: draft.allowed_emails }),
      })
      setConfig(next)
      setDraft(next)
      setSaved(true)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const enabled = Boolean(draft?.enabled)
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold">Exact request capture</h2>
            <InfoTip title="Exact request capture">Temporary Super Admin control for model-transition calibration.</InfoTip>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className={`inline-flex rounded-full border px-2 py-0.5 font-medium ${enabled ? 'border-amber-200 bg-amber-50 text-amber-800' : 'border-muted bg-muted/60 text-muted-foreground'}`}>
              {enabled ? 'Capture on' : 'Capture off'}
            </span>
            {config?.updated_at && <span>Updated {fmtDateTimeShort(config.updated_at)}</span>}
            {config?.updated_by && <span>by {config.updated_by}</span>}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading || saving}>
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            Refresh
          </Button>
          <Button size="sm" onClick={() => void save()} disabled={!draft || saving}>
            {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
            Save
          </Button>
        </div>
      </div>

      {error && <div className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
      {saved && <div className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">Saved.</div>}

      {loading && !draft ? (
        <div className="mt-4 flex min-h-20 items-center justify-center rounded-md border bg-muted/30"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
      ) : draft ? (
        <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-end">
          <div className="min-w-0">
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Selected users</span>
              <span className="text-xs text-muted-foreground">{draft.allowed_emails.length}/{draft.max_allowed_emails}</span>
            </div>
            <div className="min-h-10 rounded-md border bg-muted/20 p-2">
              {draft.allowed_emails.length === 0 ? (
                <span className="text-sm text-muted-foreground">No users selected</span>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {draft.allowed_emails.map(email => (
                    <span key={email} className="inline-flex items-center gap-1 rounded-md border bg-background py-1 pl-2 pr-1 text-sm">
                      {email}
                      <button type="button" className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground" title={`Remove ${email}`} onClick={() => { setDraft({ ...draft, allowed_emails: draft.allowed_emails.filter(item => item !== email) }); setSaved(false) }}>
                        <X className="size-3" />
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
          <label className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm font-medium">
            Capture requests
            <Switch checked={enabled} onCheckedChange={(checked) => { setDraft({ ...draft, enabled: checked }); setSaved(false) }} />
          </label>
          <div className="flex gap-2 lg:col-span-2">
            <Input type="email" value={newEmail} placeholder="priya.shah@northstar.example.com" onChange={(e) => setNewEmail(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addEmail() } }} />
            <Button type="button" variant="outline" size="icon" title="Add selected user" onClick={addEmail} disabled={!newEmail.trim() || saving}><Plus className="size-4" /></Button>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function DispatcherAdminPanel({ startDate, endDate }: { startDate: string; endDate: string }) {
  const [config, setConfig] = useState<AiDispatcherConfig | null>(null)
  const [draft, setDraft] = useState<AiDispatcherConfig | null>(null)
  const [dashboard, setDashboard] = useState<AiDispatcherDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [nextConfig, nextDashboard] = await Promise.all([
        apiFetch<AiDispatcherConfig>('/api/admin/ai-dispatcher/config'),
        apiFetch<AiDispatcherDashboard>(dispatcherDashboardPath(startDate, endDate)),
      ])
      setConfig(nextConfig)
      setDraft(nextConfig)
      setDashboard(nextDashboard)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [endDate, startDate])

  useEffect(() => { void load() }, [load])

  function patchDraft(next: Partial<AiDispatcherConfig>) {
    setDraft(prev => prev ? { ...prev, ...next } : prev)
    setSaved(false)
  }

  function toggleListValue(field: 'eligible_task_classes' | 'assistive_task_classes' | 'shadow_task_classes', value: string) {
    setDraft(prev => {
      if (!prev) return prev
      const values = new Set(prev[field] ?? [])
      if (values.has(value)) values.delete(value)
      else values.add(value)
      return { ...prev, [field]: Array.from(values) }
    })
    setSaved(false)
  }

  function setDispatcherMode(nextMode: string) {
    setDraft(prev => {
      if (!prev) return prev
      const next: AiDispatcherConfig = { ...prev, mode: nextMode }
      if (nextMode === 'primary_for_eligible') {
        next.eligible_task_classes = prev.primary_eligible_task_classes?.length
          ? prev.primary_eligible_task_classes
          : prev.eligible_task_classes
        next.assistive_task_classes = prev.primary_answer_affecting_task_classes?.length
          ? prev.primary_answer_affecting_task_classes
          : prev.assistive_task_classes
      }
      return next
    })
    setSaved(false)
  }

  async function saveConfig() {
    if (!draft) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const payload = {
        mode: draft.mode,
        planner_daily_cap_cad: Number(draft.planner_daily_cap_cad),
        max_planner_calls_per_turn: Number(draft.max_planner_calls_per_turn),
        max_tool_actions: Number(draft.max_tool_actions),
        max_explicit_ticket_ids: Number(draft.max_explicit_ticket_ids),
        max_action_rows: Number(draft.max_action_rows),
        max_output_tokens: Number(draft.max_output_tokens),
        eligible_task_classes: draft.eligible_task_classes,
        assistive_task_classes: draft.assistive_task_classes,
        shadow_task_classes: draft.shadow_task_classes,
      }
      const updated = await apiFetch<AiDispatcherConfig>('/api/admin/ai-dispatcher/config', {
        method: 'PATCH',
        body: JSON.stringify(payload),
      })
      setConfig(updated)
      setDraft(updated)
      setSaved(true)
      const nextDashboard = await apiFetch<AiDispatcherDashboard>(dispatcherDashboardPath(startDate, endDate))
      setDashboard(nextDashboard)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  async function emergencyDisable() {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const updated = await apiFetch<AiDispatcherConfig>('/api/admin/ai-dispatcher/emergency-disable', { method: 'POST' })
      setConfig(updated)
      setDraft(updated)
      setSaved(true)
      const nextDashboard = await apiFetch<AiDispatcherDashboard>(dispatcherDashboardPath(startDate, endDate))
      setDashboard(nextDashboard)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const availableModes = draft?.allowed_modes?.length ? draft.allowed_modes : ['disabled', 'shadow']
  const mode = draft?.mode ?? config?.mode ?? 'disabled'
  const primaryMode = mode === 'primary_for_eligible'
  const availableClasses = primaryMode && draft?.primary_eligible_task_classes?.length
    ? draft.primary_eligible_task_classes
    : (draft?.allowed_task_classes?.length ? draft.allowed_task_classes : ['prior_result_reference', 'bulk_ticket_ids'])
  const availableAssistiveClasses = primaryMode && draft?.primary_answer_affecting_task_classes?.length
    ? draft.primary_answer_affecting_task_classes
    : (draft?.allowed_assistive_task_classes?.length ? draft.allowed_assistive_task_classes : ['prior_result_reference'])
  const availableShadowClasses = draft?.allowed_shadow_task_classes?.length
    ? draft.allowed_shadow_task_classes
    : []
  const answerAffectingAllowed = (mode === 'assistive' || primaryMode) && Boolean((draft?.assistive_task_classes ?? config?.assistive_task_classes ?? []).length)

  return (
    <div className="space-y-3">
      <div className="rounded-lg border bg-card p-4 shadow-sm">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold">AI dispatcher</h2>
              <InfoTip title="AI dispatcher">
                Super Admin controls for the bounded planner mode, caps, answer-affecting classes, and compact telemetry.
              </InfoTip>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className={`inline-flex rounded-full border px-2 py-0.5 font-medium ${mode === 'disabled' ? 'border-muted bg-muted/60 text-muted-foreground' : 'border-sky-200 bg-sky-50 text-sky-800'}`}>
                {mode.replaceAll('_', ' ')}
              </span>
              {answerAffectingAllowed && <span className="text-emerald-700">answer-affecting allowed</span>}
              {config?.updated_at && <span>Updated {fmtDateTimeShort(config.updated_at)}</span>}
              {config?.updated_by && <span>by {config.updated_by}</span>}
              {config?.emergency_disabled && <span className="text-red-700">Emergency disabled</span>}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading || saving}>
              {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              Refresh
            </Button>
            <Button size="sm" onClick={() => void saveConfig()} disabled={!draft || saving}>
              {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
              Save
            </Button>
            <Button variant="destructive" size="sm" onClick={() => void emergencyDisable()} disabled={saving || mode === 'disabled'}>
              <AlertTriangle className="size-4" />
              Disable
            </Button>
          </div>
        </div>

        {error && (
          <div className="mt-3 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
        {saved && (
          <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
            Saved.
          </div>
        )}

        {loading && !draft ? (
          <div className="mt-4 flex min-h-[8rem] items-center justify-center rounded-lg border bg-muted/30">
            <Loader2 className="size-5 animate-spin text-muted-foreground" />
          </div>
        ) : draft ? (
          <div className="mt-4 grid gap-4 xl:grid-cols-[1.05fr_1fr]">
            <div className="grid gap-3 md:grid-cols-2">
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Mode
                <select
                  className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"
                  value={draft.mode}
                  onChange={(e) => setDispatcherMode(e.target.value)}
                >
                  {availableModes.map(item => <option key={item} value={item}>{item.replaceAll('_', ' ')}</option>)}
                </select>
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Daily planner cap
                <Input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  className="h-9"
                  value={draft.planner_daily_cap_cad}
                  onChange={(e) => patchDraft({ planner_daily_cap_cad: Number(e.target.value) })}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Planner calls
                <Input
                  type="number"
                  min={1}
                  max={1}
                  className="h-9"
                  value={draft.max_planner_calls_per_turn}
                  onChange={(e) => patchDraft({ max_planner_calls_per_turn: Number(e.target.value) })}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Actions
                <Input
                  type="number"
                  min={1}
                  max={4}
                  className="h-9"
                  value={draft.max_tool_actions}
                  onChange={(e) => patchDraft({ max_tool_actions: Number(e.target.value) })}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Ticket ID cap
                <Input
                  type="number"
                  min={1}
                  max={75}
                  className="h-9"
                  value={draft.max_explicit_ticket_ids}
                  onChange={(e) => patchDraft({ max_explicit_ticket_ids: Number(e.target.value) })}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-muted-foreground">
                Row cap
                <Input
                  type="number"
                  min={1}
                  max={100}
                  className="h-9"
                  value={draft.max_action_rows}
                  onChange={(e) => patchDraft({ max_action_rows: Number(e.target.value) })}
                />
              </label>
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-md border bg-muted/20 p-3">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Eligible classes</h3>
                <div className="mt-2 grid gap-2">
                  {availableClasses.map(item => (
                    <label key={item} className="flex min-w-0 items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="size-4 rounded border"
                        checked={(draft.eligible_task_classes ?? []).includes(item)}
                        onChange={() => toggleListValue('eligible_task_classes', item)}
                      />
                      <span className="min-w-0 truncate">{item.replaceAll('_', ' ')}</span>
                    </label>
                  ))}
                </div>
              </div>
              <div className="rounded-md border bg-muted/20 p-3">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Answer-affecting classes</h3>
                <div className="mt-2 grid gap-2">
                  {availableAssistiveClasses.map(item => (
                    <label key={item} className="flex min-w-0 items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="size-4 rounded border"
                        checked={(draft.assistive_task_classes ?? []).includes(item)}
                        onChange={() => toggleListValue('assistive_task_classes', item)}
                      />
                      <span className="min-w-0 truncate">{item.replaceAll('_', ' ')}</span>
                    </label>
                  ))}
                </div>
              </div>
              <div className="rounded-md border bg-muted/20 p-3">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Shadow-only classes</h3>
                <div className="mt-2 grid gap-2">
                  {availableShadowClasses.map(item => (
                    <label key={item} className="flex min-w-0 items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="size-4 rounded border"
                        checked={(draft.shadow_task_classes ?? []).includes(item)}
                        onChange={() => toggleListValue('shadow_task_classes', item)}
                      />
                      <span className="min-w-0 truncate">{item.replaceAll('_', ' ')}</span>
                    </label>
                  ))}
                </div>
              </div>
              {draft.blocked_modes?.length > 0 && (
                <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
                  Blocked modes: {draft.blocked_modes.map(item => item.replaceAll('_', ' ')).join(', ')}
                </div>
              )}
            </div>
          </div>
        ) : null}
      </div>

      {dashboard && (
        <>
          {dashboard.warnings.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <div className="flex items-center gap-2 font-medium"><AlertTriangle className="size-4" />Partial dispatcher telemetry</div>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-xs">
                {dashboard.warnings.map(item => <li key={item}>{item}</li>)}
              </ul>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
            <UsageMetricCard compact title="Traces" value={dashboard.summary.trace_count.toLocaleString()} detail={`${dashboard.summary.eligible_turns.toLocaleString()} eligible`} icon={<Activity className="size-4" />} />
            <UsageMetricCard compact title="Planner calls" value={dashboard.summary.planner_attempted_turns.toLocaleString()} detail={`${dashboard.summary.accepted_plans.toLocaleString()} accepted`} icon={<ShieldCheck className="size-4" />} />
            <UsageMetricCard compact title="Rejected" value={dashboard.summary.vetoed_or_rejected_plans.toLocaleString()} detail={`${dashboard.summary.answer_affecting_turns.toLocaleString()} answer-affecting`} icon={<AlertTriangle className="size-4" />} />
            <UsageMetricCard compact title="p50 latency" value={fmtSeconds(dashboard.latency.p50_ms == null ? null : dashboard.latency.p50_ms / 1000)} detail={`p90 ${fmtSeconds(dashboard.latency.p90_ms == null ? null : dashboard.latency.p90_ms / 1000)}`} icon={<Clock3 className="size-4" />} />
            <UsageMetricCard compact title="Est. cost" value={fmtCad(dashboard.cost.estimated_total_cad)} detail={`${dashboard.cost.estimated_count.toLocaleString()} estimates`} icon={<DollarSign className="size-4" />} />
            <UsageMetricCard compact title="Actual cost" value={fmtCad(dashboard.cost.actual_total_cad)} detail={`${dashboard.cost.actual_count.toLocaleString()} actuals`} icon={<DollarSign className="size-4" />} />
          </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="flex flex-col gap-1 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold">Dispatcher exposure by class</h3>
                <p className="text-xs text-muted-foreground">
                  Candidate-to-answer funnel for {formatReportDate(dashboard.range.start_date)} to {formatReportDate(dashboard.range.end_date)}.
                </p>
              </div>
              <span className="text-xs text-muted-foreground">
                {dashboard.quality_join.matched_dispatcher_traces.toLocaleString()} quality-linked traces
              </span>
            </div>
            {dashboard.class_exposure.length === 0 ? (
              <div className="px-4 py-6 text-sm text-muted-foreground">No dispatcher classes are available for this range.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[1120px] text-sm">
                  <thead className="bg-muted text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Class</th>
                      <th className="px-3 py-2 text-right font-medium">Candidate</th>
                      <th className="px-3 py-2 text-right font-medium">Attempted</th>
                      <th className="px-3 py-2 text-right font-medium">Accepted</th>
                      <th className="px-3 py-2 text-right font-medium">Rejected</th>
                      <th className="px-3 py-2 text-right font-medium">Suppressed / locked</th>
                      <th className="px-3 py-2 text-right font-medium">Answer-affecting</th>
                      <th className="px-3 py-2 text-right font-medium">Weak / suspect</th>
                      <th className="px-3 py-2 text-right font-medium">Planner p50</th>
                      <th className="px-3 py-2 text-right font-medium">Turn p50</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {dashboard.class_exposure.map(row => (
                      <tr key={row.task_class} className="align-top">
                        <td className="max-w-[15rem] whitespace-normal px-3 py-2 font-medium">{row.task_class.replaceAll('_', ' ')}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{row.candidate_turns.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{row.planner_attempted_turns.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{row.accepted_plans.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{row.rejected_plans.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{row.suppressed_or_locked_turns.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right font-medium tabular-nums">{row.answer_affecting_turns.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {bucketCount(row.quality_distribution, ['weak', 'suspect', 'likely_wrong', 'unsafe']).toLocaleString()}
                          <span className="ml-1 text-xs text-muted-foreground">/ {row.quality_turns.toLocaleString()}</span>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{fmtSeconds(row.planner_latency.p50_ms == null ? null : row.planner_latency.p50_ms / 1000)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{fmtSeconds(row.turn_latency.p50_ms == null ? null : row.turn_latency.p50_ms / 1000)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <QualityBucketCard title="Mode distribution" items={dashboard.mode_distribution} empty="No dispatcher modes logged." />
            <QualityBucketCard title="Fallback reasons" items={dashboard.fallback_reasons} empty="No fallback reasons logged." />
            <QualityBucketCard title="Reason codes" items={dashboard.reason_codes} empty="No reason codes logged." />
          </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
              <div>
                <h3 className="text-sm font-semibold">Recent dispatcher traces</h3>
                <p className="text-xs text-muted-foreground">Compact metadata only.</p>
              </div>
              <span className="text-xs text-muted-foreground">{dashboard.summary.partial ? 'Partial source coverage' : 'Trace source queried'}</span>
            </div>
            {dashboard.recent_traces.length === 0 ? (
              <div className="px-4 py-6 text-sm text-muted-foreground">No dispatcher traces found.</div>
            ) : (
              <div className="max-h-[20rem] overflow-auto">
                <table className="w-full min-w-[980px] text-sm">
                  <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Time</th>
                      <th className="px-3 py-2 text-left font-medium">Mode</th>
                      <th className="px-3 py-2 text-left font-medium">Decision</th>
                      <th className="px-3 py-2 text-left font-medium">Cost / latency</th>
                      <th className="px-3 py-2 text-left font-medium">Result set</th>
                      <th className="px-3 py-2 text-left font-medium">Trace</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {dashboard.recent_traces.map((row, idx) => (
                      <tr key={`${row.trace_id ?? row.turn_id ?? idx}`} className="align-top">
                        <td className="whitespace-nowrap px-3 py-2">{fmtDateTimeShort(row.created_at)}</td>
                        <td className="px-3 py-2">{row.mode?.replaceAll('_', ' ') ?? 'unknown'}</td>
                        <td className="px-3 py-2">
                          <div>{row.accepted ? 'Accepted' : row.planner_attempted ? 'Rejected' : row.fallback_reason || 'Skipped'}</div>
                          <div className="max-w-[18rem] truncate text-xs text-muted-foreground">{row.reason_codes?.join(', ') || row.fallback_reason || '—'}</div>
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          <div>{fmtCad(row.actual_cost_cad ?? row.estimated_cost_cad)}</div>
                          <div>{fmtSeconds(row.latency_ms == null ? null : row.latency_ms / 1000)}</div>
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          <div>{row.result_set_saved ? 'Saved' : 'No save'}</div>
                          <div>{row.result_set_comparison ? 'Compared' : 'No compare'}</div>
                          {row.assistive_task_class && <div>{row.assistive_task_class.replaceAll('_', ' ')}</div>}
                        </td>
                        <td className="px-3 py-2">
                          <div className="max-w-[16rem] space-y-1 text-xs text-muted-foreground">
                            <div className="truncate">trace {row.trace_id ?? '—'}</div>
                            <div className="truncate">turn {row.turn_id ?? '—'}</div>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function QualityTelemetryTab({ active }: { active: boolean }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const defaultEndDate = useMemo(() => dateInputValueInBusinessTime(0), [])
  const defaultStartDate = useMemo(() => addDaysToDateInput(defaultEndDate, -6), [defaultEndDate])
  const [startDate, setStartDate] = useState(() => isDateInputValue(searchParams.get('start_date')) ? searchParams.get('start_date')! : defaultStartDate)
  const [endDate, setEndDate] = useState(() => isDateInputValue(searchParams.get('end_date')) ? searchParams.get('end_date')! : defaultEndDate)
  const [quality, setQuality] = useState(() => searchParams.get('quality') ?? '')
  const [eventType, setEventType] = useState(() => searchParams.get('event_type') ?? '')
  const [risk, setRisk] = useState(() => searchParams.get('risk') ?? '')
  const [validatorAction, setValidatorAction] = useState(() => searchParams.get('validator_action') ?? '')
  const [repairAction, setRepairAction] = useState(() => searchParams.get('repair_action') ?? '')
  const [stopReason, setStopReason] = useState(() => searchParams.get('stop_reason') ?? '')
  const [modelSurface, setModelSurface] = useState(() => searchParams.get('model_surface') ?? '')
  const [requesterEmail, setRequesterEmail] = useState(() => searchParams.get('requester_email') ?? '')
  const [reasonTag, setReasonTag] = useState(() => searchParams.get('reason_tag') ?? '')
  const [limit, setLimit] = useState(() => qualityLimitFromParam(searchParams.get('limit')))
  const [data, setData] = useState<QualityTelemetryDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!active) return
    const params = new URLSearchParams()
    params.set('start_date', startDate)
    params.set('end_date', endDate)
    params.set('limit', String(limit))
    if (quality) params.set('quality', quality)
    if (eventType) params.set('event_type', eventType)
    if (risk) params.set('risk', risk)
    if (validatorAction) params.set('validator_action', validatorAction)
    if (repairAction) params.set('repair_action', repairAction)
    if (stopReason) params.set('stop_reason', stopReason)
    if (modelSurface) params.set('model_surface', modelSurface)
    if (requesterEmail.trim()) params.set('requester_email', requesterEmail.trim())
    if (reasonTag) params.set('reason_tag', reasonTag)
    setSearchParams(params, { replace: true })
  }, [active, endDate, eventType, limit, modelSurface, quality, reasonTag, repairAction, requesterEmail, risk, setSearchParams, startDate, stopReason, validatorAction])

  const load = useCallback(async () => {
    if (!active) return
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      params.set('start_date', startDate)
      params.set('end_date', endDate)
      params.set('limit', String(limit))
      if (quality) params.set('quality', quality)
      if (eventType) params.set('event_type', eventType)
      if (risk) params.set('risk', risk)
      if (validatorAction) params.set('validator_action', validatorAction)
      if (repairAction) params.set('repair_action', repairAction)
      if (stopReason) params.set('stop_reason', stopReason)
      if (modelSurface) params.set('model_surface', modelSurface)
      if (requesterEmail.trim()) params.set('requester_email', requesterEmail.trim())
      if (reasonTag) params.set('reason_tag', reasonTag)
      const result = await apiFetch<QualityTelemetryDashboard>(`/api/admin/quality/dashboard?${params.toString()}`)
      setData(result)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [active, endDate, eventType, limit, modelSurface, quality, reasonTag, repairAction, requesterEmail, risk, startDate, stopReason, validatorAction])

  useEffect(() => { if (active) void load() }, [active, load])

  const qualityChart = useMemo(
    () => (data?.quality_distribution ?? []).map(item => ({
      name: item.name.replace('_', ' '),
      count: item.count,
    })),
    [data],
  )
  const trendChart = useMemo(
    () => (data?.trend ?? []).map(row => ({
      day: String(row.day ?? ''),
      events: Number(row.events ?? 0),
      weak: Number(row.weak ?? 0) + Number(row.suspect ?? 0),
      wrong: Number(row.likely_wrong ?? 0) + Number(row.unsafe ?? 0),
    })),
    [data],
  )
  const topRisks = data?.risk_classes.slice(0, 6) ?? []
  const topActions = data?.validator_actions.slice(0, 6) ?? []
  const topStops = data?.repair_stop_reasons.slice(0, 6) ?? []
  const suspectShare = data ? qualityPct(data.summary.suspect_event_count, data.summary.quality_event_count) : '0%'

  if (!active) return null

  return (
    <div className="space-y-4">
      <ModelTransitionCapturePanel />
      <DispatcherAdminPanel startDate={startDate} endDate={endDate} />

      <div className="flex flex-col gap-3 rounded-lg border bg-card p-3 shadow-sm lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold">Quality telemetry</h2>
            <InfoTip title="Quality telemetry">
              Super Admin-only diagnostic view of validator, review, repair-loop, latency, model, and
              cost signals. It is read-only and does not expose raw chat transcripts in aggregate rows.
            </InfoTip>
          </div>
          <p className="text-xs text-muted-foreground">
            Aggregated validation, repair, latency, and cost signals for recent assistant turns.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            From
            <Input type="date" className="h-9 w-[10.5rem]" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            To
            <Input type="date" className="h-9 w-[10.5rem]" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Event
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={eventType} onChange={(e) => setEventType(e.target.value)}>
              <option value="">All</option>
              <option value="auto_validator">Auto validator</option>
              <option value="manual_review">Manual review</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Quality
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={quality} onChange={(e) => setQuality(e.target.value)}>
              <option value="">All</option>
              <option value="good">Good</option>
              <option value="weak">Weak</option>
              <option value="suspect">Suspect</option>
              <option value="likely_wrong">Likely wrong</option>
              <option value="insufficient_evidence">Insufficient evidence</option>
              <option value="unsafe">Unsafe</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Rows
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              <option value={10}>10</option>
              <option value={25}>25</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Risk
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={risk} onChange={(e) => setRisk(e.target.value)}>
              <option value="">All</option>
              <option value="false_no_data">False no data</option>
              <option value="stale_data">Stale data</option>
              <option value="tool_error">Tool error</option>
              <option value="raw_artifact">Raw artifact</option>
              <option value="weak_sources">Weak sources</option>
              <option value="context_loss">Context loss</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Validator action
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={validatorAction} onChange={(e) => setValidatorAction(e.target.value)}>
              <option value="">All</option>
              <option value="rerun_bq">Rerun BQ</option>
              <option value="zendesk_fallback">Zendesk fallback</option>
              <option value="drive_exact_retry">Drive exact retry</option>
              <option value="rebuild_scoped_evidence">Rebuild evidence</option>
              <option value="rewrite_with_existing_sources">Rewrite from sources</option>
              <option value="none">None</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Repair
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={repairAction} onChange={(e) => setRepairAction(e.target.value)}>
              <option value="">All</option>
              <option value="rerun_bq">Rerun BQ</option>
              <option value="zendesk_fallback">Zendesk fallback</option>
              <option value="drive_exact_retry">Drive exact retry</option>
              <option value="rebuild_scoped_evidence">Rebuild evidence</option>
              <option value="rewrite_with_existing_sources">Rewrite from sources</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Stop reason
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={stopReason} onChange={(e) => setStopReason(e.target.value)}>
              <option value="">All</option>
              <option value="accepted">Accepted</option>
              <option value="max_validations">Max validations</option>
              <option value="max_repairs">Max repairs</option>
              <option value="repeated_action">Repeated action</option>
              <option value="no_progress">No progress</option>
              <option value="scope_drift">Scope drift</option>
              <option value="validator_failure">Validator failure</option>
              <option value="routine_answer">Routine answer</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Surface
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={modelSurface} onChange={(e) => setModelSurface(e.target.value)}>
              <option value="">All</option>
              <option value="answer_validator">Answer validator</option>
              <option value="chat_quality_review">Quality review</option>
              <option value="repair_escalation">Repair escalation</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Requester
            <Input
              className="h-9 w-[14rem]"
              placeholder="alex.morgan@northstar.example.com"
              value={requesterEmail}
              onChange={(e) => setRequesterEmail(e.target.value)}
            />
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            Review reason
            <select className="h-9 rounded-md border bg-background px-3 text-sm text-foreground" value={reasonTag} onChange={(e) => setReasonTag(e.target.value)}>
              <option value="">All reasons</option>
              <option value="missed_context">Missed context</option>
              <option value="wrong_source">Wrong source</option>
              <option value="no_data">No data</option>
              <option value="too_long">Too long</option>
              <option value="slow">Slow</option>
              <option value="needs_ticket_resolution">Needs ticket resolution</option>
            </select>
          </label>
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && !data ? (
        <div className="flex min-h-[20rem] items-center justify-center rounded-lg border bg-card">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : data ? (
        <>
          {data.warnings.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <div className="flex items-center gap-2 font-medium"><AlertTriangle className="size-4" />Partial telemetry warning</div>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-xs">
                {data.warnings.map(item => <li key={item}>{item}</li>)}
              </ul>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-8">
            <UsageMetricCard
              compact
              title="Quality events (raw)"
              value={data.summary.quality_event_count.toLocaleString()}
              detail={`${data.source_counts?.quality_events_before_filters ?? data.summary.quality_event_count} before filters`}
              icon={<ShieldCheck className="size-4" />}
            />
            <UsageMetricCard
              compact
              title="Unique answers"
              value={data.summary.unique_answer_count.toLocaleString()}
              detail={`${data.summary.turn_count.toLocaleString()} logged turns`}
              icon={<AlertTriangle className="size-4" />}
            />
            <UsageMetricCard
              compact
              title="Auto validator"
              value={data.summary.auto_validator_event_count.toLocaleString()}
              detail={`${suspectShare} suspect/unsafe events`}
              icon={<CheckCircle2 className="size-4" />}
            />
            <UsageMetricCard
              compact
              title="Manual reviews"
              value={data.summary.manual_review_event_count.toLocaleString()}
              detail="Improve Answer reviews"
              icon={<Activity className="size-4" />}
            />
            <UsageMetricCard compact title="Duplicate events" value={data.summary.duplicate_event_count.toLocaleString()} detail="Same trace/turn reviewed again" icon={<Activity className="size-4" />} />
            <UsageMetricCard compact title="Conflicting verdicts" value={data.summary.conflicting_verdict_count.toLocaleString()} detail="Different outcomes on one answer" icon={<AlertTriangle className="size-4" />} />
            <UsageMetricCard
              compact
              title="Avg latency"
              value={fmtSeconds(data.latency.avg_ms == null ? null : data.latency.avg_ms / 1000)}
              detail={`p95 ${fmtSeconds(data.latency.p95_ms == null ? null : data.latency.p95_ms / 1000)}`}
              icon={<Clock3 className="size-4" />}
            />
            <UsageMetricCard
              compact
              title="Est. cost"
              value={fmtCad(data.cost.estimated_total_cad)}
              detail={`${data.cost.count.toLocaleString()} costed events`}
              icon={<DollarSign className="size-4" />}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <h3 className="text-sm font-semibold">Quality distribution</h3>
              <p className="mb-3 text-xs text-muted-foreground">Validator/review outcomes in the selected range.</p>
              {qualityChart.length === 0 ? (
                <div className="flex h-[240px] items-center justify-center text-sm text-muted-foreground">No quality events found.</div>
              ) : (
                <div className="h-[240px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={qualityChart}>
                      <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                      <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                      <Tooltip />
                      <Bar dataKey="count" name="Events" fill="#2563eb" radius={[3, 3, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
            <div className="rounded-lg border bg-card p-4 shadow-sm">
              <h3 className="text-sm font-semibold">Quality trend</h3>
              <p className="mb-3 text-xs text-muted-foreground">
                Daily event volume from {formatReportDate(data.range.start_date)} to {formatReportDate(data.range.end_date)}.
              </p>
              {trendChart.length === 0 ? (
                <div className="flex h-[240px] items-center justify-center text-sm text-muted-foreground">No trend data found.</div>
              ) : (
                <div className="h-[240px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={trendChart}>
                      <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                      <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                      <Tooltip />
                      <Legend />
                      <Bar dataKey="events" name="All events" fill="#2563eb" radius={[3, 3, 0, 0]} />
                      <Bar dataKey="weak" name="Weak/suspect" fill="#f59e0b" radius={[3, 3, 0, 0]} />
                      <Bar dataKey="wrong" name="Wrong/unsafe" fill="#dc2626" radius={[3, 3, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <QualityBucketCard title="Top risks" items={topRisks} empty="No risk classes logged." />
            <QualityBucketCard title="Validator actions" items={topActions} empty="No validator actions logged." />
            <QualityBucketCard title="Repair stop reasons" items={topStops} empty="No repair loop stops logged." />
          </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="flex flex-col gap-1 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold">Weak-answer review queue</h3>
                <p className="text-xs text-muted-foreground">One compact row per answer. No question or answer text is included.</p>
              </div>
              <span className="text-xs text-muted-foreground">
                {data.review_queue.answer_count.toLocaleString()} answers
                {data.review_queue.selected_reason_tag ? ` · ${data.review_queue.selected_reason_tag.replaceAll('_', ' ')}` : ''}
              </span>
            </div>
            {data.review_queue.rows.length === 0 ? (
              <div className="px-4 py-6 text-sm text-muted-foreground">
                {reasonTag ? 'No review answers match the selected reason in this date range.' : 'No weak or conflicting answers were found in this date range.'}
              </div>
            ) : (
              <div className="max-h-[28rem] overflow-auto">
                <table className="w-full min-w-[1120px] text-sm">
                  <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Time</th>
                      <th className="px-3 py-2 text-left font-medium">Quality</th>
                      <th className="px-3 py-2 text-left font-medium">Review reasons</th>
                      <th className="px-3 py-2 text-left font-medium">Evidence</th>
                      <th className="px-3 py-2 text-left font-medium">Size / latency</th>
                      <th className="px-3 py-2 text-left font-medium">Ticket</th>
                      <th className="px-3 py-2 text-left font-medium">Inspect</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {data.review_queue.rows.map((row, idx) => (
                      <tr key={`${row.trace_id ?? row.turn_id ?? idx}`} className="align-top">
                        <td className="whitespace-nowrap px-3 py-2">{fmtDateTimeShort(row.created_at)}</td>
                        <td className="px-3 py-2">
                          <div className="flex max-w-[14rem] flex-wrap gap-1">
                            {row.qualities.map(item => (
                              <span key={item} className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${qualityLabelClass(item)}`}>
                                {item.replaceAll('_', ' ')}
                              </span>
                            ))}
                            {row.conflicting_verdict && <span className="text-xs font-medium text-red-700">conflict</span>}
                          </div>
                        </td>
                        <td className="max-w-[22rem] whitespace-normal px-3 py-2 text-xs">
                          {row.reason_tags.length > 0 ? row.reason_tags.map(item => item.replaceAll('_', ' ')).join(', ') : 'Other review signal'}
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          <div>{row.sources_count.toLocaleString()} sources</div>
                          <div>{row.tickets_count.toLocaleString()} tickets</div>
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          <div>{row.answer_chars.toLocaleString()} chars</div>
                          <div>{fmtSeconds(row.latency_ms == null ? null : row.latency_ms / 1000)}</div>
                        </td>
                        <td className="px-3 py-2">{row.target_ticket_id ?? '—'}</td>
                        <td className="px-3 py-2">
                          {row.trace_id || row.turn_id ? (
                            <div className="max-w-[15rem] space-y-1 text-xs">
                              <Link className="font-medium text-primary hover:underline" to={chatLogsLink(row.trace_id, row.turn_id)}>Open Chat Logs</Link>
                              <div className="truncate text-muted-foreground">trace {row.trace_id ?? '—'}</div>
                              <div className="truncate text-muted-foreground">turn {row.turn_id ?? '—'}</div>
                            </div>
                          ) : <span className="text-xs text-muted-foreground">IDs unavailable</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            <div className="min-w-0 rounded-lg border bg-card shadow-sm">
              <div className="border-b px-4 py-3">
                <h3 className="text-sm font-semibold">Universal Chat stage timing</h3>
                <p className="text-xs text-muted-foreground">
                  {data.stage_timings.measured_turn_count.toLocaleString()} of {data.stage_timings.turn_count.toLocaleString()} turns measured
                  {data.stage_timings.partial_measured_turn_count > 0 ? ` · ${data.stage_timings.partial_measured_turn_count.toLocaleString()} partial` : ''}.
                </p>
              </div>
              {!data.stage_timings.available ? (
                <div className="px-4 py-6 text-sm text-muted-foreground">No stage timing telemetry is available for this date range.</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px] text-sm">
                    <thead className="bg-muted text-xs text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">Stage</th>
                        <th className="px-3 py-2 text-right font-medium">Measured</th>
                        <th className="px-3 py-2 text-right font-medium">Missing</th>
                        <th className="px-3 py-2 text-right font-medium">p50</th>
                        <th className="px-3 py-2 text-right font-medium">p90</th>
                        <th className="px-3 py-2 text-right font-medium">Max</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {data.stage_timings.stages.map(row => (
                        <tr key={row.stage}>
                          <td className="max-w-[14rem] whitespace-normal px-3 py-2 font-medium">{row.stage.replaceAll('_', ' ')}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{row.count.toLocaleString()}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{(data.stage_timings.missing_by_stage.find(item => item.stage === row.stage)?.count ?? 0).toLocaleString()}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{fmtSeconds(row.p50_ms == null ? null : row.p50_ms / 1000)}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{fmtSeconds(row.p90_ms == null ? null : row.p90_ms / 1000)}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{fmtSeconds(row.max_ms == null ? null : row.max_ms / 1000)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="border-t px-4 py-3 text-xs text-muted-foreground">{data.stage_timings.caveat}</p>
            </div>

            <div className="min-w-0 rounded-lg border bg-card shadow-sm">
              <div className="border-b px-4 py-3">
                <h3 className="text-sm font-semibold">Upstream and fallback provenance</h3>
                <p className="text-xs text-muted-foreground">Compact failures and recoveries, without response bodies or customer text.</p>
              </div>
              {data.error_provenance.event_count === 0 ? (
                <div className="px-4 py-6 text-sm text-muted-foreground">No upstream or fallback events were recorded in this date range.</div>
              ) : (
                <div className="space-y-4 p-4">
                  <div className="grid grid-cols-2 gap-3 text-sm">
                    <div><div className="text-xs text-muted-foreground">Events</div><div className="text-xl font-semibold tabular-nums">{data.error_provenance.event_count.toLocaleString()}</div></div>
                    <div><div className="text-xs text-muted-foreground">Fallback used</div><div className="text-xl font-semibold tabular-nums">{data.error_provenance.fallback_used_count.toLocaleString()}</div></div>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <QualityBucketList title="Sources" items={data.error_provenance.sources} />
                    <QualityBucketList title="Outcomes" items={data.error_provenance.outcomes} />
                    <QualityBucketList title="Endpoint classes" items={data.error_provenance.endpoint_classes} />
                    <QualityBucketList title="Status codes" items={data.error_provenance.status_codes} />
                    <div className="sm:col-span-2"><QualityBucketList title="Reason codes" items={data.error_provenance.reason_codes} /></div>
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="rounded-lg border bg-card shadow-sm">
            <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
              <div>
                <h3 className="text-sm font-semibold">Recent suspect events</h3>
                <p className="text-xs text-muted-foreground">Compact metadata only. Use IDs to inspect related Chat Logs.</p>
              </div>
              <span className="text-xs text-muted-foreground">
                {data.coverage.partial ? 'Partial source coverage' : 'Full source query completed'}
              </span>
            </div>
            {data.recent_events.length === 0 ? (
              <div className="px-4 py-6 text-sm text-muted-foreground">No quality events matched the current filters.</div>
            ) : (
              <div className="max-h-[28rem] overflow-auto">
                <table className="w-full min-w-[1180px] text-sm">
                  <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Time</th>
                      <th className="px-3 py-2 text-left font-medium">Mode</th>
                      <th className="px-3 py-2 text-left font-medium">Quality</th>
                      <th className="px-3 py-2 text-left font-medium">Risk / action</th>
                      <th className="px-3 py-2 text-left font-medium">Repair stop</th>
                      <th className="px-3 py-2 text-left font-medium">Model</th>
                      <th className="px-3 py-2 text-left font-medium">Latency / cost</th>
                      <th className="px-3 py-2 text-left font-medium">Trace</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {data.recent_events.map((event, idx) => (
                      <tr key={`${event.trace_id ?? event.turn_id ?? idx}`} className="align-top">
                        <td className="px-3 py-2 whitespace-nowrap">{fmtDateTimeShort(event.created_at)}</td>
                        <td className="px-3 py-2">{event.mode ?? 'unknown'}</td>
                        <td className="px-3 py-2">
                          <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${qualityLabelClass(event.quality)}`}>
                            {(event.quality ?? 'unknown').replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-3 py-2">
                          <div className="max-w-[16rem] space-y-1">
                            <div>{event.validator_action ?? event.repair_action ?? '—'}</div>
                            {event.risk_classes && event.risk_classes.length > 0 && (
                              <div className="text-xs text-muted-foreground">{event.risk_classes.join(', ')}</div>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2">{event.repair_stop_reason ?? '—'}</td>
                        <td className="px-3 py-2">
                          <div className="max-w-[12rem] truncate">{event.model_surface ?? event.model ?? '—'}</div>
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          <div>{event.latency_ms == null ? '—' : fmtSeconds(event.latency_ms / 1000)}</div>
                          <div>{fmtCad(event.estimated_cost_cad)}</div>
                        </td>
                        <td className="px-3 py-2">
                          <div className="max-w-[16rem] space-y-1 text-xs text-muted-foreground">
                            <div className="truncate">trace {event.trace_id ?? '—'}</div>
                            <div className="truncate">turn {event.turn_id ?? '—'}</div>
                            <Link className="text-primary hover:underline" to={chatLogsLink(event.trace_id, event.turn_id)}>Open Chat Logs</Link>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="rounded-lg border bg-muted/30 px-4 py-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Caveats</h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted-foreground">
              {data.caveats.map(item => <li key={item}>{item}</li>)}
            </ul>
          </div>
        </>
      ) : null}
    </div>
  )
}

function QualityBucketCard({
  title,
  items,
  empty,
}: {
  title: string
  items: QualityTelemetryBucket[]
  empty: string
}) {
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <h3 className="text-sm font-semibold">{title}</h3>
      {items.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">{empty}</p>
      ) : (
        <div className="mt-3 space-y-2">
          {items.map(item => (
            <div key={item.name} className="flex items-center justify-between gap-3 rounded-md bg-muted/40 px-3 py-2 text-sm">
              <span className="min-w-0 truncate">{item.name.replaceAll('_', ' ')}</span>
              <span className="font-semibold tabular-nums">{item.count.toLocaleString()}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function QualityBucketList({ title, items }: { title: string; items: QualityTelemetryBucket[] }) {
  return (
    <div className="min-w-0">
      <h4 className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{title}</h4>
      {items.length === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">None recorded.</p>
      ) : (
        <dl className="mt-2 space-y-1.5 text-sm">
          {items.slice(0, 6).map(item => (
            <div key={item.name} className="flex items-start justify-between gap-3">
              <dt className="min-w-0 whitespace-normal break-words">{item.name.replaceAll('_', ' ')}</dt>
              <dd className="font-semibold tabular-nums">{item.count.toLocaleString()}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Universal Chat logs tab (Super Admin only)
// ---------------------------------------------------------------------------

interface ChatLogEntry {
  created_at: string | null
  conversation_id: string
  turn_id: string
  trace_id: string | null
  owner_email: string | null
  owner_display_name?: string | null
  owner_role: string | null
  ticket_id: number | null
  target_ticket_id: number | null
  question: string
  answer: string
  sources_count: number
  tickets_count: number
  answer_chars: number
  latency_ms: number | null
  gcs_payload_uri: string | null
}

function clipText(text: string | null | undefined, limit: number): string {
  const value = (text ?? '').trim()
  if (value.length <= limit) return value
  return `${value.slice(0, limit).trimEnd()}...`
}

function ChatLogsTab() {
  const [searchParams] = useSearchParams()
  const targetTurnId = searchParams.get('turn_id')
  const targetTraceId = searchParams.get('trace_id')
  const [logs, setLogs] = useState<ChatLogEntry[]>([])
  const [limit, setLimit] = useState(() => targetTurnId || targetTraceId ? 100 : 25)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [questionWidthPct, setQuestionWidthPct] = useState(42)
  const [paneHeights, setPaneHeights] = useState<Record<string, number>>({})

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch<{ logs: ChatLogEntry[]; limit: number }>(
        `/api/admin/chat-logs/recent?limit=${limit}`,
      )
      setLogs(r.logs)
      const target = r.logs.find(log => (
        (targetTurnId && log.turn_id === targetTurnId)
        || (targetTraceId && log.trace_id === targetTraceId)
      ))
      setExpanded(target ? new Set([target.turn_id]) : new Set())
      if (target) {
        window.requestAnimationFrame(() => {
          window.requestAnimationFrame(() => {
            document.getElementById(`chat-log-${encodeURIComponent(target.turn_id)}`)?.scrollIntoView({ block: 'center' })
          })
        })
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [limit, targetTraceId, targetTurnId])

  useEffect(() => { void load() }, [load])

  const allExpanded = logs.length > 0 && expanded.size === logs.length
  const toggleAll = () => {
    setExpanded(allExpanded ? new Set() : new Set(logs.map(log => log.turn_id)))
  }
  const toggleOne = (turnId: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(turnId)) next.delete(turnId)
      else next.add(turnId)
      return next
    })
  }

  const getPaneStyle = (turnId: string): CSSProperties => ({
    overflow: 'auto',
    minWidth: 0,
    height: `${paneHeights[turnId] ?? 300}px`,
    maxWidth: '100%',
    maxHeight: '75vh',
  })

  const startSplitDrag = (event: ReactMouseEvent<HTMLDivElement>) => {
    const wrapper = event.currentTarget.parentElement
    if (!wrapper) return
    const rect = wrapper.getBoundingClientRect()
    const onMove = (moveEvent: MouseEvent) => {
      const pct = ((moveEvent.clientX - rect.left) / rect.width) * 100
      setQuestionWidthPct(Math.max(20, Math.min(70, pct)))
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const startHeightDrag = (turnId: string, event: ReactMouseEvent<HTMLDivElement>) => {
    const startY = event.clientY
    const startHeight = paneHeights[turnId] ?? 300
    const onMove = (moveEvent: MouseEvent) => {
      const nextHeight = Math.max(140, Math.min(900, startHeight + moveEvent.clientY - startY))
      setPaneHeights(prev => ({ ...prev, [turnId]: nextHeight }))
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  return (
    <div className="max-w-5xl space-y-4">
      <div className="rounded-lg border bg-card shadow-sm">
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-b">
          <div>
            <h2 className="font-medium">Universal Chat logs</h2>
            <p className="text-sm text-muted-foreground mt-0.5">
              Recent durable logs for checking what is being captured.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button
              variant="outline"
              size="sm"
              className="h-8"
              onClick={toggleAll}
              disabled={loading || logs.length === 0}
            >
              {allExpanded ? 'Collapse all' : 'Expand all'}
            </Button>
            <select
              className="h-8 px-2 text-sm rounded border bg-background text-foreground"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={10}>10</option>
              <option value={25}>25</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
            </select>
            <Button variant="ghost" size="sm" onClick={load} disabled={loading} className="h-8 w-8 p-0">
              <RefreshCw className={`size-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>

        {error && (
          <div className="px-4 py-2 text-xs text-destructive flex items-center gap-1.5 border-b">
            <AlertTriangle className="size-3" /> {error}
          </div>
        )}

        <div className="divide-y">
          {loading && (
            <div className="flex items-center gap-2 px-4 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Loading...
            </div>
          )}
          {!loading && logs.length === 0 && (
            <div className="px-4 py-4 text-sm text-muted-foreground">No logs found.</div>
          )}
          {!loading && logs.map((log) => (
            <div id={`chat-log-${encodeURIComponent(log.turn_id)}`} key={log.turn_id} className="px-4 py-3 space-y-2 min-w-0 overflow-hidden scroll-mt-4">
              <div className="flex items-start justify-between gap-3 min-w-0">
                <button
                  type="button"
                  onClick={() => toggleOne(log.turn_id)}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0 text-left text-xs text-muted-foreground hover:text-foreground"
                >
                  {expanded.has(log.turn_id) ? (
                    <ChevronDown className="size-3 shrink-0" />
                  ) : (
                    <ChevronUp className="size-3 shrink-0 -rotate-90" />
                  )}
                  <span className="font-medium text-foreground">
                    {log.created_at ? new Date(log.created_at).toLocaleString() : 'Unknown time'}
                  </span>
                  <span>{personLabel({ display_name: log.owner_display_name, email: log.owner_email })}</span>
                  <span>{log.tickets_count} tickets</span>
                  <span>{log.sources_count} sources</span>
                  {log.latency_ms != null && <span>{Math.round(log.latency_ms / 100) / 10}s</span>}
                  {log.target_ticket_id && <span>ticket #{log.target_ticket_id}</span>}
                </button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs shrink-0"
                  onClick={() => toggleOne(log.turn_id)}
                >
                  {expanded.has(log.turn_id) ? 'Collapse' : 'Show'}
                </Button>
              </div>
              {!expanded.has(log.turn_id) && (
                <div className="grid gap-2 text-sm md:grid-cols-2">
                  <div className="min-w-0 truncate">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mr-2">
                      Question
                    </span>
                    {clipText(log.question, 180)}
                  </div>
                  <div className="min-w-0 truncate">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mr-2">
                      Answer
                    </span>
                    {clipText(log.answer, 220)}
                  </div>
                </div>
              )}
              {expanded.has(log.turn_id) && (
                <div className="space-y-1">
                  <div
                    className="grid min-w-0 gap-0 overflow-hidden"
                    style={{
                      gridTemplateColumns: `${questionWidthPct}% 8px minmax(0, 1fr)`,
                    }}
                  >
                    <div className="min-w-0 rounded-l border border-r-0 bg-muted/30 px-3 py-2" style={getPaneStyle(log.turn_id)}>
                      <div className="sticky top-0 -mx-3 -mt-2 mb-1 border-b bg-muted px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        Question
                      </div>
                      <p className="text-sm whitespace-pre-wrap break-words leading-6">{log.question}</p>
                    </div>
                    <div
                      className="cursor-col-resize border-y bg-border hover:bg-primary/40"
                      title="Drag to resize question and answer width"
                      onMouseDown={startSplitDrag}
                    />
                    <div className="min-w-0 rounded-r border border-l-0 bg-background px-3 py-2" style={getPaneStyle(log.turn_id)}>
                      <div className="sticky top-0 -mx-3 -mt-2 mb-1 border-b bg-background px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        Answer
                      </div>
                      <p className="text-sm whitespace-pre-wrap break-words leading-6">{log.answer}</p>
                    </div>
                  </div>
                  <div
                    className="h-2 cursor-row-resize rounded bg-border hover:bg-primary/40"
                    title="Drag to resize this log pane vertically"
                    onMouseDown={(event) => startHeightDrag(log.turn_id, event)}
                  />
                </div>
              )}
              <div className="text-[11px] text-muted-foreground break-all">
                conversation {log.conversation_id} · turn {log.turn_id}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function AdminPanelPage({
  initialTab = 'costs',
  initialInsightMode = 'esip',
}: {
  initialTab?: AdminPanelTab
  initialInsightMode?: InsightMode
}) {
  const { loading, isAdmin, role: callerRole } = useAuth()
  const navigate = useNavigate()
  const isSuperAdmin = callerRole === 'super_admin'
  const [activeTab, setActiveTab] = useState<AdminPanelTab>(initialTab)
  const [maintLoading, setMaintLoading] = useState(true)
  const [maintEnabled, setMaintEnabled] = useState(false)
  const [maintSaving, setMaintSaving] = useState(false)
  const [maintError, setMaintError] = useState<string | null>(null)

  useEffect(() => {
    if (!isAdmin) return
    setMaintLoading(true)
    setMaintError(null)
    void apiFetch<{ enabled: boolean }>('/api/admin/maintenance')
      .then((r) => setMaintEnabled(r.enabled))
      .catch((e) => setMaintError((e as Error).message))
      .finally(() => setMaintLoading(false))
  }, [isAdmin])

  useEffect(() => {
    const nextTab = !isSuperAdmin && (initialTab === 'changes' || initialTab === 'quality' || initialTab === 'chat-logs') ? 'costs' : initialTab
    setActiveTab(nextTab)
  }, [initialTab, isSuperAdmin])

  const selectAdminTab = useCallback((nextTab: string) => {
    const tab = nextTab as AdminPanelTab
    setActiveTab(tab)
    navigate(ADMIN_TAB_PATHS[tab] ?? '/admin/panel')
  }, [navigate])

  async function toggleMaintenance(next: boolean) {
    setMaintSaving(true)
    setMaintError(null)
    try {
      const r = await apiFetch<{ enabled: boolean }>('/api/admin/maintenance', {
        method: 'PATCH',
        body: JSON.stringify({ enabled: next }),
      })
      setMaintEnabled(r.enabled)
    } catch (e) {
      setMaintError((e as Error).message)
    } finally {
      setMaintSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="min-h-[40vh] flex items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (!isAdmin) return <Navigate to="/" replace />

  return (
    <PageScroll>
    <div className="p-6 space-y-6">
      <div className="flex items-center gap-2">
        <LayoutDashboard className="size-6 text-primary" />
        <h1 className="text-xl font-semibold">Admin Panel</h1>
      </div>

      <Tabs value={activeTab} onValueChange={selectAdminTab}>
        {/* Tab switcher — compact, left-aligned */}
        <TabsList className="inline-flex h-9 max-w-full gap-0.5 overflow-x-auto rounded-lg p-1">
          <TabsTrigger value="costs" className="gap-1.5 px-4">
            <DollarSign className="size-3.5" /> Costs
          </TabsTrigger>
          <TabsTrigger value="system" className="gap-1.5 px-4">
            <ShieldCheck className="size-3.5" /> System
          </TabsTrigger>
          <TabsTrigger value="notifications" className="gap-1.5 px-4">
            <Bell className="size-3.5" /> Notifications
          </TabsTrigger>
          <TabsTrigger value="users" className="gap-1.5 px-4">
            <ShieldCheck className="size-3.5" /> Users
          </TabsTrigger>
        <TabsTrigger value="usage" className="gap-1.5 px-4">
            <Activity className="size-3.5" /> Insights
        </TabsTrigger>
          <TabsTrigger value="impact" className="gap-1.5 px-4">
            <TrendingUp className="size-3.5" /> Impact
          </TabsTrigger>
          {isSuperAdmin && (
            <TabsTrigger value="changes" className="gap-1.5 px-4">
              <Bell className="size-3.5" /> What’s New
            </TabsTrigger>
          )}
          {isSuperAdmin && (
            <TabsTrigger value="quality" className="gap-1.5 px-4">
              <ShieldCheck className="size-3.5" /> Quality
            </TabsTrigger>
          )}
          {isSuperAdmin && (
            <TabsTrigger value="chat-logs" className="gap-1.5 px-4">
              <MessageSquareText className="size-3.5" /> Chat logs
            </TabsTrigger>
          )}
        </TabsList>

        {/* System tab — constrained width */}
        <TabsContent value="system" className="mt-4">
          <div className="max-w-2xl space-y-5">
          <div className="rounded-lg border bg-card text-card-foreground shadow-sm">
            <div className="p-4 border-b">
              <h2 className="font-medium">Maintenance mode</h2>
              <p className="text-sm text-muted-foreground mt-1">
                When active, non-admin users are blocked on each API call. Use System Status to see whether ESIP
                is open. The switch turns normal operation on or off (maintenance mode).
              </p>
            </div>
            <div className="p-4 space-y-4">
              {maintLoading && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" /> Loading…
                </div>
              )}
              {maintError && (
                <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {maintError}
                </div>
              )}
              {!maintLoading && (
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="text-sm font-medium">System Status</span>
                      <span
                        className={`text-sm font-semibold tabular-nums ${
                          maintEnabled ? 'text-orange-600' : 'text-green-600'
                        }`}
                      >
                        {maintEnabled ? 'OFF' : 'ON'}
                      </span>
                    </div>
                    <div className="space-y-0.5 text-xs text-muted-foreground">
                      <p>
                        <span className="font-medium text-green-700">ON</span>
                        {' = ESIP open for allowed users'}
                      </p>
                      <p>
                        <span className="font-medium text-orange-700">OFF</span>
                        {' = maintenance lock (admins only)'}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    {/*
                      Badge = system live (inverse of maintenance flag).
                      maintEnabled true → maintenance on → system OFF (orange).
                    */}
                    <span
                      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
                        maintEnabled
                          ? 'bg-orange-100 text-orange-900 border border-orange-200/80'
                          : 'bg-green-100 text-green-800 border border-green-200/80'
                      }`}
                    >
                      <span
                        className={`size-1.5 shrink-0 rounded-full ${maintEnabled ? 'bg-orange-500' : 'bg-green-500'}`}
                      />
                      {maintEnabled ? 'System paused' : 'System running'}
                    </span>
                    <Switch
                      checked={!maintEnabled}
                      disabled={maintSaving}
                      title="When on, the platform is open (maintenance mode off)"
                      aria-label="System available (maintenance mode off)"
                      onCheckedChange={(systemOn) => void toggleMaintenance(!systemOn)}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="rounded-lg border bg-muted/30 p-4 space-y-3">
            <h2 className="text-sm font-medium text-muted-foreground">Quick links</h2>
            <div className="flex flex-wrap gap-2">
              {isSuperAdmin && (
                <Button variant="outline" size="sm" asChild>
                  <Link to="/admin/feedback" className="gap-2">
                    <ShieldCheck className="size-4" /> Feedbacks
                  </Link>
                </Button>
              )}
              <Button variant="outline" size="sm" asChild>
                <Link to="/admin/sources" className="gap-2">
                  <Database className="size-4" /> Data sources
                </Link>
              </Button>
              <Button variant="outline" size="sm" asChild>
                <Link to="/admin/architecture" className="gap-2">
                  <Network className="size-4" /> Architecture
                </Link>
              </Button>
            </div>
          </div>
          </div>{/* end max-w-2xl */}
        </TabsContent>

        {/* Costs tab */}
        <TabsContent value="costs" className="mt-4">
          <CostsTab />
        </TabsContent>

        {/* Usage Insights tab */}
        <TabsContent value="usage" className="mt-4">
          <UsageInsightsTab isSuperAdmin={isSuperAdmin} initialMode={initialInsightMode} />
        </TabsContent>
        <TabsContent value="impact" className="mt-4">
          <ImpactTab />
        </TabsContent>

        {isSuperAdmin && (
          <TabsContent value="changes" className="mt-4">
            <WhatsNewAdminTab />
          </TabsContent>
        )}

        {isSuperAdmin && (
          <TabsContent value="quality" className="mt-4">
            <QualityTelemetryTab active={activeTab === 'quality'} />
          </TabsContent>
        )}

        {/* Notifications tab */}
        <TabsContent value="notifications" className="mt-4">
          <div className="max-w-2xl">
            <NotificationsTab />
          </div>
        </TabsContent>

        {/* Users tab */}
        <TabsContent value="users" className="mt-4">
          <UsersTab />
        </TabsContent>

        {isSuperAdmin && (
          <TabsContent value="chat-logs" className="mt-4">
            <ChatLogsTab />
          </TabsContent>
        )}
      </Tabs>
    </div>
    </PageScroll>
  )
}
