import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { Database, RefreshCw, Loader2, CheckCircle2, XCircle, Clock, Play, Pause, History } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { PageScroll } from '@/components/PageScroll'
import { useAuth } from '@/contexts/AuthContext'

interface DataSource {
  display_name: string
  type: 'vertex_ai' | 'bigquery' | 'zendesk_api'
  enabled: boolean
  sync_enabled?: boolean
  doc_count?: number
  last_sync?: string
  exclude_path_prefixes?: string[]
}

const GREEN_SWITCH_CLASS =
  'data-[state=checked]:bg-emerald-500 dark:data-[state=checked]:bg-emerald-500'

type SourcesMap = Record<string, DataSource>

interface SchedulerJob {
  job_id: string
  display_name: string
  purpose: string
  state: string
  enabled: boolean
  schedule: string
  schedule_time?: string | null
  time_zone: string
  last_attempt_time?: string | null
  active_run_running?: boolean
  active_run_progress?: string | null
  active_run_heartbeat_age_sec?: number | null
}

interface SyncRun {
  id: string
  run_id?: string
  job_id: string
  trigger: string
  status: string
  started_at?: string
  finished_at?: string | null
  duration_sec?: number | null
  summary?: Record<string, unknown>
  source_results?: Record<string, unknown>
  error_count?: number
  error_message?: string | null
  events?: SyncEvent[]
}

interface SyncEvent {
  id: string
  timestamp?: string
  level?: string
  stage?: string
  source_id?: string | null
  message?: string
  details?: Record<string, unknown>
}

function syncRunBadgeVariant(status: string): 'default' | 'destructive' | 'outline' | 'secondary' {
  if (status === 'success') return 'default'
  if (status === 'failed' || status === 'blocked') return 'destructive'
  if (status === 'partial_success') return 'secondary'
  return 'outline'
}

function syncRunStatusLabel(status: string): string {
  if (status === 'partial_success') return 'partial success'
  return status
}

function syncEventBadgeVariant(level?: string): 'default' | 'destructive' | 'outline' | 'secondary' {
  if (level === 'ERROR') return 'destructive'
  if (level === 'WARNING') return 'secondary'
  return 'outline'
}

const KB_ORDER = [
  'zendesk_operations_articles',
  'zendesk_support_articles',
  'reference_support_docs',
  'gdrive_operations_job_aids',
  'gdrive_support_repository',
]

const TICKETS_ORDER = [
  'tickets_zendesk',
  'tickets_bq',
]

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('en-CA', {
    timeZone: 'America/New_York',
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function formatMaybeDate(value?: string | null): string {
  if (!value) return 'Never'
  return formatDate(value)
}

function formatDuration(seconds?: number | null): string {
  if (seconds == null) return 'In progress'
  if (seconds < 60) return `${seconds}s`
  const mins = Math.floor(seconds / 60)
  const secs = seconds % 60
  return `${mins}m ${secs}s`
}

function summaryText(summary?: Record<string, unknown>): string {
  if (!summary) return ''
  const parts = Object.entries(summary)
    .filter(([, value]) => value !== null && value !== undefined && typeof value !== 'object')
    .map(([key, value]) => `${key}: ${String(value)}`)
  return parts.slice(0, 6).join(' | ')
}

export function AdminSourcesPage() {
  const { loading: authLoading, isAdmin } = useAuth()
  const [sources, setSources]     = useState<SourcesMap>({})
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState<string | null>(null)
  const [toggling, setToggling]   = useState<string | null>(null)
  const [pathDrafts, setPathDrafts] = useState<Record<string, string>>({})
  const [savingPaths, setSavingPaths] = useState<Record<string, boolean>>({})
  const [pathErrors, setPathErrors] = useState<Record<string, string>>({})
  const [schedulers, setSchedulers] = useState<SchedulerJob[]>([])
  const [schedulerDrafts, setSchedulerDrafts] = useState<Record<string, string>>({})
  const [schedulerBusy, setSchedulerBusy] = useState<string | null>(null)
  const [syncRuns, setSyncRuns] = useState<SyncRun[]>([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [selectedRun, setSelectedRun] = useState<SyncRun | null>(null)
  const [runDialogOpen, setRunDialogOpen] = useState(false)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<SourcesMap>('/api/admin/sources')
      setSources(data)
      const schedulerData = await apiFetch<SchedulerJob[]>('/api/admin/schedulers')
      setSchedulers(schedulerData)
      const schedulerDraftsNext: Record<string, string> = {}
      for (const job of schedulerData) {
        schedulerDraftsNext[job.job_id] = job.schedule_time ?? ''
      }
      setSchedulerDrafts(schedulerDraftsNext)
      const drafts: Record<string, string> = {}
      for (const [id, source] of Object.entries(data)) {
        drafts[id] = (source.exclude_path_prefixes ?? []).join('\n')
      }
      setPathDrafts(drafts)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  async function loadSyncRuns() {
    setRunsLoading(true)
    try {
      const data = await apiFetch<SyncRun[]>('/api/admin/sync-runs?days=30&limit=50')
      setSyncRuns(data)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRunsLoading(false)
    }
  }

  useEffect(() => {
    if (!isAdmin) return
    void load()
    void loadSyncRuns()
  }, [isAdmin])

  useEffect(() => {
    if (!isAdmin) return
    const hasActiveRun = schedulers.some(job => job.active_run_running)
    if (!hasActiveRun) return
    const timer = window.setInterval(() => {
      void load()
      void loadSyncRuns()
    }, 10000)
    return () => window.clearInterval(timer)
  }, [isAdmin, schedulers])

  async function updateScheduler(jobId: string, body: { enabled?: boolean; schedule_time?: string }) {
    setSchedulerBusy(jobId)
    try {
      const updated = await apiFetch<SchedulerJob>(
        `/api/admin/schedulers/${jobId}`,
        { method: 'PATCH', body: JSON.stringify(body) },
      )
      setSchedulers(prev => prev.map(job => job.job_id === jobId ? updated : job))
      setSchedulerDrafts(prev => ({ ...prev, [jobId]: updated.schedule_time ?? '' }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSchedulerBusy(null)
    }
  }

  async function runSchedulerNow(jobId: string) {
    setSchedulerBusy(jobId)
    try {
      await apiFetch(`/api/admin/schedulers/${jobId}/run-now`, { method: 'POST' })
      if (jobId === 'esip-drive-sync') {
        await new Promise(resolve => window.setTimeout(resolve, 1500))
      }
      await load()
      await loadSyncRuns()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSchedulerBusy(null)
    }
  }

  async function openRun(runId: string) {
    setRunDialogOpen(true)
    try {
      const detail = await apiFetch<SyncRun>(`/api/admin/sync-runs/${runId}`)
      setSelectedRun(detail)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  async function toggleSearch(sourceId: string, enabled: boolean) {
    setToggling(`search:${sourceId}`)
    const previous = sources[sourceId]
    setSources(prev => ({
      ...prev,
      [sourceId]: { ...prev[sourceId], enabled },
    }))
    try {
      const updated = await apiFetch<DataSource & { source_id: string }>(
        `/api/admin/sources/${sourceId}`,
        { method: 'PATCH', body: JSON.stringify({ enabled }) }
      )
      setSources(prev => ({
        ...prev,
        [sourceId]: { ...prev[sourceId], enabled: updated.enabled },
      }))
    } catch (e) {
      if (previous) {
        setSources(prev => ({ ...prev, [sourceId]: previous }))
      }
      setError((e as Error).message)
    } finally {
      setToggling(null)
    }
  }

  async function toggleSync(sourceId: string, sync_enabled: boolean) {
    setToggling(`sync:${sourceId}`)
    const previous = sources[sourceId]
    setSources(prev => ({
      ...prev,
      [sourceId]: { ...prev[sourceId], sync_enabled },
    }))
    try {
      const updated = await apiFetch<DataSource & { source_id: string }>(
        `/api/admin/sources/${sourceId}/sync`,
        { method: 'PATCH', body: JSON.stringify({ sync_enabled }) }
      )
      setSources(prev => ({
        ...prev,
        [sourceId]: {
          ...prev[sourceId],
          sync_enabled: updated.sync_enabled ?? sync_enabled,
        },
      }))
    } catch (e) {
      if (previous) {
        setSources(prev => ({ ...prev, [sourceId]: previous }))
      }
      setError((e as Error).message)
    } finally {
      setToggling(null)
    }
  }

  async function savePathExcludes(sourceId: string) {
    setPathErrors(prev => ({ ...prev, [sourceId]: '' }))
    setSavingPaths(prev => ({ ...prev, [sourceId]: true }))
    const raw = pathDrafts[sourceId] ?? ''
    const prefixes = raw
      .split('\n')
      .map(v => v.trim())
      .filter(Boolean)
    try {
      const updated = await apiFetch<DataSource & { source_id: string }>(
        `/api/admin/sources/${sourceId}/paths`,
        { method: 'PATCH', body: JSON.stringify({ exclude_path_prefixes: prefixes }) },
      )
      setSources(prev => ({
        ...prev,
        [sourceId]: {
          ...prev[sourceId],
          exclude_path_prefixes: updated.exclude_path_prefixes ?? [],
        },
      }))
      setPathDrafts(prev => ({
        ...prev,
        [sourceId]: (updated.exclude_path_prefixes ?? []).join('\n'),
      }))
    } catch (e) {
      setPathErrors(prev => ({ ...prev, [sourceId]: (e as Error).message }))
    } finally {
      setSavingPaths(prev => ({ ...prev, [sourceId]: false }))
    }
  }

  async function runSourceSyncNow(sourceId: string) {
    setToggling(`source-sync:${sourceId}`)
    try {
      await apiFetch(`/api/admin/sources/${sourceId}/sync-now`, {
        method: 'POST',
        body: JSON.stringify({ max_files_per_source: 10 }),
      })
      await load()
      await loadSyncRuns()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setToggling(null)
    }
  }

  function sortByPreferredOrder(
    entries: [string, DataSource][],
    preferredOrder: string[],
  ): [string, DataSource][] {
    const rank = new Map(preferredOrder.map((id, idx) => [id, idx]))
    return [...entries].sort((a, b) => {
      const ai = rank.get(a[0])
      const bi = rank.get(b[0])
      if (ai != null && bi != null) return ai - bi
      if (ai != null) return -1
      if (bi != null) return 1
      return a[1].display_name.localeCompare(b[1].display_name)
    })
  }

  // Group by type (stable deterministic order)
  const vertexSources = sortByPreferredOrder(
    Object.entries(sources).filter(([, s]) => s.type === 'vertex_ai'),
    KB_ORDER,
  )
  const ticketSources = sortByPreferredOrder(
    Object.entries(sources).filter(([, s]) => s.type !== 'vertex_ai'),
    TICKETS_ORDER,
  )
  const driveRunActive = schedulers.some(job => job.job_id === 'esip-drive-sync' && job.active_run_running)
  const sourceSyncActive = typeof toggling === 'string' && toggling.startsWith('source-sync:')

  if (authLoading) {
    return (
      <div className="min-h-[40vh] flex items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (!isAdmin) {
    return <Navigate to="/" replace />
  }

  return (
    <PageScroll>
      <div className="p-6 max-w-6xl mr-auto space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Database className="size-5 text-primary" />
          <h1 className="text-lg font-semibold">Data Sources</h1>
        </div>
        <Button
          variant="ghost" size="sm"
          onClick={() => void load()}
          disabled={loading}
          className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className={`size-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm py-8 justify-center">
          <Loader2 className="size-4 animate-spin" />
          Loading...
        </div>
      )}

      {error && (
        <div className="rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {!loading && !error && (
        <>
          <SourceGroup
            title="Knowledge Base (Vertex AI)"
            entries={vertexSources}
            toggling={toggling}
            onToggleSearch={toggleSearch}
            onToggleSync={toggleSync}
            pathDrafts={pathDrafts}
            savingPaths={savingPaths}
            pathErrors={pathErrors}
            driveRunActive={driveRunActive || sourceSyncActive}
            onPathDraftChange={(sourceId, value) => {
              setPathDrafts(prev => ({ ...prev, [sourceId]: value }))
            }}
            onSavePathExcludes={savePathExcludes}
            onRunSourceSyncNow={runSourceSyncNow}
          />
          <SourceGroup
            title="Tickets"
            entries={ticketSources}
            toggling={toggling}
            onToggleSearch={toggleSearch}
            onToggleSync={toggleSync}
            pathDrafts={pathDrafts}
            savingPaths={savingPaths}
            pathErrors={pathErrors}
            driveRunActive={driveRunActive || sourceSyncActive}
            onPathDraftChange={(sourceId, value) => {
              setPathDrafts(prev => ({ ...prev, [sourceId]: value }))
            }}
            onSavePathExcludes={savePathExcludes}
            onRunSourceSyncNow={runSourceSyncNow}
          />

          <SchedulerPanel
            jobs={schedulers}
            drafts={schedulerDrafts}
            busyJob={schedulerBusy}
            onDraftChange={(jobId, value) => setSchedulerDrafts(prev => ({ ...prev, [jobId]: value }))}
            onToggle={(jobId, enabled) => void updateScheduler(jobId, { enabled })}
            onSaveTime={(jobId) => void updateScheduler(jobId, { schedule_time: schedulerDrafts[jobId] })}
            onRunNow={(jobId) => void runSchedulerNow(jobId)}
          />

          <SyncHistoryPanel
            runs={syncRuns}
            loading={runsLoading}
            onRefresh={() => void loadSyncRuns()}
            onOpenRun={(runId) => void openRun(runId)}
          />

          <div className="rounded-lg border bg-muted/40 px-4 py-3 text-xs text-muted-foreground space-y-1">
            <p>Changes apply instantly. No deploy needed.</p>
            <p>`Search` controls whether the source is used by chat, ticket similarity, Drive exact search, and analytics routing.</p>
            <p>`Sync` controls whether future sync runs ingest or refresh that source. Ticket ingest and ticket BQ/OCR/embeddings can be paused independently.</p>
            <p>Machine IDs [in brackets] are immutable. Display names can be changed freely.</p>
          </div>

          <SyncRunDialog
            open={runDialogOpen}
            run={selectedRun}
            onOpenChange={(open) => {
              setRunDialogOpen(open)
              if (!open) setSelectedRun(null)
            }}
          />
        </>
      )}
      </div>
    </PageScroll>
  )
}

interface SourceGroupProps {
  title: string
  entries: [string, DataSource][]
  toggling: string | null
  onToggleSearch: (id: string, enabled: boolean) => void
  onToggleSync: (id: string, syncEnabled: boolean) => void
  pathDrafts: Record<string, string>
  savingPaths: Record<string, boolean>
  pathErrors: Record<string, string>
  driveRunActive: boolean
  onPathDraftChange: (id: string, value: string) => void
  onSavePathExcludes: (id: string) => void
  onRunSourceSyncNow: (id: string) => void
}

interface SchedulerPanelProps {
  jobs: SchedulerJob[]
  drafts: Record<string, string>
  busyJob: string | null
  onDraftChange: (jobId: string, value: string) => void
  onToggle: (jobId: string, enabled: boolean) => void
  onSaveTime: (jobId: string) => void
  onRunNow: (jobId: string) => void
}

function SchedulerPanel({
  jobs,
  drafts,
  busyJob,
  onDraftChange,
  onToggle,
  onSaveTime,
  onRunNow,
}: SchedulerPanelProps) {
  if (!jobs.length) return null
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 px-1">
        <Clock className="size-4 text-muted-foreground" />
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
          Nightly Schedules
        </h2>
      </div>
      <div className="rounded-lg border divide-y bg-card">
        {jobs.map((job) => {
          const busy = busyJob === job.job_id
          const runActive = Boolean(job.active_run_running)
          const controlsBusy = busy || runActive
          const draft = drafts[job.job_id] ?? ''
          const dirty = draft !== (job.schedule_time ?? '')
          const isDaily = Boolean(job.schedule_time)
          return (
            <div key={job.job_id} className="px-4 py-3 space-y-3">
              <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{job.display_name}</span>
                    <Badge variant={job.enabled ? 'default' : 'outline'}>{job.enabled ? 'Enabled' : 'Paused'}</Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{job.purpose}</p>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                    <span className="font-mono">[{job.job_id}]</span>
                    <span>{job.schedule || 'No schedule'}</span>
                    <span>{job.time_zone}</span>
                    <span>Last attempt: {formatMaybeDate(job.last_attempt_time)}</span>
                    {runActive && (
                      <span className="text-amber-700">
                        Running: {job.active_run_progress || 'Sync in progress'}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {isDaily && (
                    <>
                      <Input
                        value={draft}
                        onChange={(e) => onDraftChange(job.job_id, e.target.value)}
                        placeholder="HH:mm"
                        className="h-8 w-24 text-sm"
                        disabled={controlsBusy}
                      />
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={controlsBusy || !dirty}
                        onClick={() => onSaveTime(job.job_id)}
                      >
                        Save time
                      </Button>
                    </>
                  )}
                  <Button
                    type="button"
                    size="sm"
                    variant={job.enabled ? 'outline' : 'default'}
                    disabled={controlsBusy}
                    onClick={() => onToggle(job.job_id, !job.enabled)}
                    className="gap-1.5"
                  >
                    {job.enabled ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
                    {job.enabled ? 'Pause cron' : 'Enable cron'}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={controlsBusy}
                    onClick={() => onRunNow(job.job_id)}
                    className="gap-1.5"
                  >
                    {controlsBusy ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
                    {runActive ? 'Running' : 'Run now'}
                  </Button>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

interface SyncHistoryPanelProps {
  runs: SyncRun[]
  loading: boolean
  onRefresh: () => void
  onOpenRun: (runId: string) => void
}

function SyncHistoryPanel({ runs, loading, onRefresh, onOpenRun }: SyncHistoryPanelProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between px-1">
        <div className="flex items-center gap-2">
          <History className="size-4 text-muted-foreground" />
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
            Sync History
          </h2>
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={onRefresh} disabled={loading} className="h-8 gap-1.5 text-xs">
          <RefreshCw className={`size-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh history
        </Button>
      </div>
      <div className="rounded-lg border bg-card overflow-hidden">
        {runs.length === 0 && (
          <div className="px-4 py-6 text-sm text-muted-foreground">
            No sync runs recorded yet.
          </div>
        )}
        {runs.length > 0 && (
          <div className="divide-y">
            {runs.map((run) => (
              <button
                key={run.id}
                type="button"
                onClick={() => onOpenRun(run.id)}
                className="w-full px-4 py-3 text-left hover:bg-muted/50 transition-colors"
              >
                <div className="grid gap-2 lg:grid-cols-[170px_180px_100px_minmax(0,1fr)_90px] lg:items-center">
                  <div className="text-xs text-muted-foreground">{formatMaybeDate(run.started_at)}</div>
                  <div className="text-sm font-medium">{run.job_id}</div>
                  <Badge variant={syncRunBadgeVariant(run.status)}>
                    {syncRunStatusLabel(run.status)}
                  </Badge>
                  <div className="truncate text-xs text-muted-foreground">
                    {summaryText(run.summary) || run.error_message || run.trigger}
                  </div>
                  <div className="text-xs text-muted-foreground">{formatDuration(run.duration_sec)}</div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SyncRunDialog({
  open,
  run,
  onOpenChange,
}: {
  open: boolean
  run: SyncRun | null
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{run ? `${run.job_id} — ${syncRunStatusLabel(run.status)}` : 'Sync run'}</DialogTitle>
        </DialogHeader>
        {!run && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading run log...
          </div>
        )}
        {run && (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2 text-sm">
              <InfoLine label="Started" value={formatMaybeDate(run.started_at)} />
              <InfoLine label="Finished" value={formatMaybeDate(run.finished_at)} />
              <InfoLine label="Duration" value={formatDuration(run.duration_sec)} />
              <InfoLine label="Trigger" value={run.trigger} />
              <InfoLine label="Errors" value={String(run.error_count ?? 0)} />
              <InfoLine label="Run ID" value={run.id} mono />
            </div>
            {run.error_message && (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
                {run.error_message}
              </div>
            )}
            {run.status === 'partial_success' && typeof run.summary?.ocr_backlog_message === 'string' && (
              <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
                {run.summary.ocr_backlog_message}
              </div>
            )}
            <div>
              <h3 className="text-sm font-medium mb-2">Summary</h3>
              <pre className="rounded-md border bg-muted/40 p-3 text-xs overflow-x-auto">
                {JSON.stringify(run.summary ?? {}, null, 2)}
              </pre>
            </div>
            <SourceResultsPanel sourceResults={run.source_results} />
            <div>
              <h3 className="text-sm font-medium mb-2">Events</h3>
              <div className="rounded-md border divide-y">
                {(run.events ?? []).length === 0 && (
                  <div className="p-3 text-sm text-muted-foreground">No events recorded.</div>
                )}
                {(run.events ?? []).map((event) => (
                  <div key={event.id} className="p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-muted-foreground">{formatMaybeDate(event.timestamp)}</span>
                      <Badge variant={syncEventBadgeVariant(event.level)}>{event.level ?? 'INFO'}</Badge>
                      <span className="font-mono text-xs text-muted-foreground">{event.stage}</span>
                      {event.source_id && <span className="font-mono text-xs text-muted-foreground">[{event.source_id}]</span>}
                    </div>
                    <div className="mt-1">{event.message}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function SourceResultsPanel({ sourceResults }: { sourceResults?: Record<string, unknown> }) {
  if (!sourceResults || Object.keys(sourceResults).length === 0) return null

  const entries = Object.entries(sourceResults)
  return (
    <div>
      <h3 className="text-sm font-medium mb-2">Source Results</h3>
      <div className="rounded-md border divide-y">
        {entries.map(([sourceId, value]) => (
          <SourceResultItem key={sourceId} sourceId={sourceId} value={value} />
        ))}
      </div>
    </div>
  )
}

function SourceResultItem({ sourceId, value }: { sourceId: string; value: unknown }) {
  const result = isRecord(value) ? value : {}
  const warnings = asRecordArray(result.file_warnings)
  const errors = asRecordArray(result.file_errors)
  const skippedTicketIds = asArray(result.skipped_ticket_ids)
  const issueCount = warnings.length + errors.length + skippedTicketIds.length

  return (
    <div className="p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs">{sourceId}</span>
        {typeof result.status === 'string' && <Badge variant="outline">{result.status}</Badge>}
        {Number(result.errors ?? 0) > 0 && <Badge variant="destructive">{String(result.errors)} errors</Badge>}
        {issueCount > 0 && <Badge variant="outline">{issueCount} item issue{issueCount === 1 ? '' : 's'}</Badge>}
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-2 text-xs text-muted-foreground">
        {compactMetric('added', result.added)}
        {compactMetric('updated', result.updated)}
        {compactMetric('deleted', result.deleted)}
        {compactMetric('skipped', result.skipped)}
        {compactMetric('skipped_no_text', result.skipped_no_text)}
        {compactMetric('remaining_after_limit', result.remaining_after_limit)}
        {compactMetric('pending_total', result.pending_total)}
        {compactMetric('selected_for_run', result.selected_for_run)}
        {compactMetric('skipped_tickets', result.skipped_tickets)}
      </div>
      {skippedTicketIds.length > 0 && (
        <IssueList
          title="Skipped tickets"
          items={skippedTicketIds.map((id) => ({ label: `Ticket ${String(id)}`, reason: 'comments fetch failed; existing BQ rows preserved' }))}
        />
      )}
      {warnings.length > 0 && <IssueList title="File warnings" items={warnings.map(issueItem)} />}
      {errors.length > 0 && <IssueList title="File errors" items={errors.map(issueItem)} destructive />}
    </div>
  )
}

function compactMetric(label: string, value: unknown) {
  if (value === undefined || value === null || value === '') return null
  if (typeof value === 'number' && value === 0) return null
  return (
    <div key={label}>
      <span className="font-medium text-foreground">{label}:</span> {String(value)}
    </div>
  )
}

function IssueList({
  title,
  items,
  destructive = false,
}: {
  title: string
  items: Array<{ label: string; reason: string; detail?: string }>
  destructive?: boolean
}) {
  return (
    <div className="mt-3">
      <div className={destructive ? 'text-xs font-medium text-destructive' : 'text-xs font-medium'}>{title}</div>
      <div className="mt-1 rounded border bg-muted/30 divide-y">
        {items.map((item, index) => (
          <div key={`${item.label}-${index}`} className="p-2 text-xs">
            <div className="font-medium break-words">{item.label}</div>
            <div className="text-muted-foreground break-words">{item.reason}</div>
            {item.detail && <div className="font-mono text-[11px] text-muted-foreground break-all">{item.detail}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}

function issueItem(item: Record<string, unknown>) {
  const label = String(item.file_name || item.file_id || 'Unknown file')
  const path = item.source_path ? String(item.source_path) : ''
  return {
    label,
    reason: String(item.reason || 'unknown'),
    detail: path || String(item.file_id || ''),
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function InfoLine({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={mono ? 'font-mono text-xs break-all' : 'text-sm'}>{value}</div>
    </div>
  )
}

function SourceGroup({
  title,
  entries,
  toggling,
  onToggleSearch,
  onToggleSync,
  pathDrafts,
  savingPaths,
  pathErrors,
  driveRunActive,
  onPathDraftChange,
  onSavePathExcludes,
  onRunSourceSyncNow,
}: SourceGroupProps) {
  if (entries.length === 0) return null
  return (
    <div className="space-y-2">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide px-1">
        {title}
      </h2>
      <div className="rounded-lg border divide-y bg-card">
        {entries.map(([id, source]) => (
          <SourceRow
            key={id}
            sourceId={id}
            source={source}
            searchToggling={toggling === `search:${id}`}
            syncToggling={toggling === `sync:${id}`}
            sourceSyncToggling={toggling === `source-sync:${id}`}
            driveRunActive={driveRunActive}
            onToggleSearch={onToggleSearch}
            onToggleSync={onToggleSync}
            onRunSourceSyncNow={onRunSourceSyncNow}
            pathDraft={pathDrafts[id] ?? ''}
            pathSaving={Boolean(savingPaths[id])}
            pathError={pathErrors[id] ?? ''}
            onPathDraftChange={onPathDraftChange}
            onSavePathExcludes={onSavePathExcludes}
          />
        ))}
      </div>
    </div>
  )
}

interface SourceRowProps {
  sourceId: string
  source: DataSource
  searchToggling: boolean
  syncToggling: boolean
  sourceSyncToggling: boolean
  driveRunActive: boolean
  onToggleSearch: (id: string, enabled: boolean) => void
  onToggleSync: (id: string, syncEnabled: boolean) => void
  onRunSourceSyncNow: (id: string) => void
  pathDraft: string
  pathSaving: boolean
  pathError: string
  onPathDraftChange: (id: string, value: string) => void
  onSavePathExcludes: (id: string) => void
}

function SourceRow({
  sourceId,
  source,
  searchToggling,
  syncToggling,
  sourceSyncToggling,
  driveRunActive,
  onToggleSearch,
  onToggleSync,
  onRunSourceSyncNow,
  pathDraft,
  pathSaving,
  pathError,
  onPathDraftChange,
  onSavePathExcludes,
}: SourceRowProps) {
  const isDriveSource = source.type === 'vertex_ai' && sourceId.startsWith('gdrive_')
  const currentPaths = (source.exclude_path_prefixes ?? []).join('\n')
  const pathDirty = pathDraft.trim() !== currentPaths.trim()
  const syncEnabled = source.sync_enabled ?? true
  const sourceSyncBusy = sourceSyncToggling || driveRunActive

  return (
    <div className="px-4 py-3 space-y-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{source.display_name}</span>
            {source.enabled
              ? <CheckCircle2 className="size-3.5 text-emerald-500 shrink-0" />
              : <XCircle className="size-3.5 text-muted-foreground/50 shrink-0" />
            }
          </div>
          <div className="flex items-center gap-3 mt-0.5 text-xs text-muted-foreground">
            <span className="font-mono">[{sourceId}]</span>
            {source.doc_count != null && (
              <span>{source.doc_count.toLocaleString()} docs</span>
            )}
            {source.last_sync && (
              <span>synced: {formatDate(source.last_sync)}</span>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 sm:justify-end">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Search</span>
            <Switch
              checked={source.enabled}
              onCheckedChange={(checked: boolean) => onToggleSearch(sourceId, checked)}
              disabled={searchToggling}
              aria-label={`Toggle search for ${source.display_name}`}
              className={GREEN_SWITCH_CLASS}
            />
            {searchToggling && <Loader2 className="size-4 animate-spin text-muted-foreground shrink-0" />}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Sync</span>
            <Switch
              checked={syncEnabled}
              onCheckedChange={(checked: boolean) => onToggleSync(sourceId, checked)}
              disabled={syncToggling}
              aria-label={`Toggle sync for ${source.display_name}`}
              className={GREEN_SWITCH_CLASS}
            />
            {syncToggling && <Loader2 className="size-4 animate-spin text-muted-foreground shrink-0" />}
          </div>
          {isDriveSource && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={sourceSyncBusy || !syncEnabled}
              onClick={() => onRunSourceSyncNow(sourceId)}
              className="h-8 gap-1.5"
            >
              {sourceSyncBusy ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
              {driveRunActive ? 'Running' : 'Sync 10'}
            </Button>
          )}
        </div>
      </div>

      {isDriveSource && (
        <div className="pl-12 space-y-2">
          <label className="block text-xs font-medium text-muted-foreground">
            Exclude path prefixes (one per line)
          </label>
          <textarea
            value={pathDraft}
            onChange={(e) => onPathDraftChange(sourceId, e.target.value)}
            rows={3}
            placeholder={'2. General/Archived'}
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
          />
          {pathError && (
            <p className="text-xs text-destructive">{pathError}</p>
          )}
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onSavePathExcludes(sourceId)}
              disabled={pathSaving || !pathDirty}
            >
              {pathSaving ? 'Saving...' : 'Save path rules'}
            </Button>
            <span className="text-xs text-muted-foreground">
              Matches are prefix-based, case-insensitive, and apply on next sync.
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
