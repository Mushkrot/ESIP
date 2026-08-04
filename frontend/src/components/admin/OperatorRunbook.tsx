import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  BookOpenCheck,
  CheckCircle2,
  ChevronRight,
  Clock3,
  ExternalLink,
  Filter,
  Info,
  KeyRound,
  Loader2,
  Map as MapIcon,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  ShieldCheck,
  StopCircle,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

interface RunbookCategory {
  id: string
  label: string
  description: string
}

interface RunbookStep {
  title: string
  detail: string
  dashboard_path?: BrowserPath
  manual_path?: BrowserPath
}

interface BrowserPath {
  label: string
  url: string
  navigation: string
}

interface RunbookPortal {
  label: string
  location: string
  url: string
}

export interface RunbookProcedure {
  id: string
  title: string
  summary: string
  category: string
  risk: 'safe' | 'caution' | 'approval_required' | 'emergency'
  systems: string[]
  estimated_time: string
  prerequisites: string[]
  steps: RunbookStep[]
  verify: string[]
  stop_conditions: string[]
  rollback: string[]
  portals: RunbookPortal[]
  related_diagrams: string[]
}

export interface OperatorRunbookPayload {
  schema_version: number
  title: string
  description: string
  access: {
    title: string
    instruction: string
    access_note: string
  }
  categories: RunbookCategory[]
  procedures: RunbookProcedure[]
}

interface DiagramReference {
  id: string
  title: string
}

interface OperatorRunbookProps {
  runbookUrl?: string
  previewData?: OperatorRunbookPayload
  diagrams: DiagramReference[]
  onOpenDiagram: (diagramId: string) => void
}

const riskPresentation = {
  safe: {
    label: 'Safe inspection',
    icon: ShieldCheck,
    badge: 'border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    accent: 'border-l-emerald-500',
  },
  caution: {
    label: 'Caution',
    icon: AlertTriangle,
    badge: 'border-amber-600/30 bg-amber-500/10 text-amber-800 dark:text-amber-300',
    accent: 'border-l-amber-500',
  },
  approval_required: {
    label: 'Approval required',
    icon: ShieldAlert,
    badge: 'border-orange-600/30 bg-orange-500/10 text-orange-800 dark:text-orange-300',
    accent: 'border-l-orange-500',
  },
  emergency: {
    label: 'Emergency',
    icon: AlertOctagon,
    badge: 'border-destructive/30 bg-destructive/10 text-destructive',
    accent: 'border-l-destructive',
  },
} as const

function searchableText(procedure: RunbookProcedure): string {
  return [
    procedure.title,
    procedure.summary,
    procedure.category,
    procedure.risk,
    ...procedure.systems,
    ...procedure.prerequisites,
    ...procedure.steps.flatMap((step) => [
      step.title,
      step.detail,
      step.dashboard_path?.label ?? '',
      step.dashboard_path?.navigation ?? '',
      step.manual_path?.label ?? '',
      step.manual_path?.navigation ?? '',
    ]),
    ...procedure.verify,
    ...procedure.stop_conditions,
    ...procedure.rollback,
    ...procedure.portals.flatMap((portal) => [portal.label, portal.location, portal.url]),
  ].join(' ').toLowerCase()
}

function EmptyRunbook({ title = 'Operator Runbook unavailable', message }: { title?: string; message: string }) {
  return (
    <div className="flex min-h-72 flex-col items-center justify-center rounded-xl border border-dashed bg-muted/15 px-6 text-center">
      <BookOpenCheck className="size-8 text-muted-foreground" />
      <h3 className="mt-4 text-sm font-semibold">{title}</h3>
      <p className="mt-1 max-w-md text-sm leading-relaxed text-muted-foreground">{message}</p>
    </div>
  )
}

function SectionList({
  title,
  icon: Icon,
  items,
  tone = 'neutral',
}: {
  title: string
  icon: typeof Info
  items: string[]
  tone?: 'neutral' | 'success' | 'danger'
}) {
  if (!items.length) return null
  const toneClasses = tone === 'success'
    ? 'border-emerald-600/20 bg-emerald-500/5'
    : tone === 'danger'
      ? 'border-destructive/20 bg-destructive/5'
      : 'border-border bg-muted/15'
  const iconClasses = tone === 'success'
    ? 'text-emerald-700 dark:text-emerald-300'
    : tone === 'danger'
      ? 'text-destructive'
      : 'text-muted-foreground'
  return (
    <section className={`rounded-xl border p-4 ${toneClasses}`}>
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Icon className={`size-4 ${iconClasses}`} />
        {title}
      </h3>
      <ul className="mt-3 space-y-2 text-sm leading-relaxed">
        {items.map((item) => (
          <li key={item} className="flex gap-2.5">
            <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-current opacity-50" />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function BrowserPathCard({
  path,
  variant,
}: {
  path: BrowserPath
  variant: 'dashboard' | 'manual'
}) {
  const heading = variant === 'dashboard' ? 'ESIP Admin Dashboard' : 'Manual cloud console'
  const classes = variant === 'dashboard'
    ? 'border-primary/25 bg-primary/5 hover:bg-primary/10'
    : 'border-border bg-muted/25 hover:bg-muted/45'
  return (
    <a
      href={path.url}
      target="_blank"
      rel="noreferrer"
      className={`group block rounded-lg border p-3 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${classes}`}
    >
      <span className="flex items-center justify-between gap-3">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{heading}</span>
        <ExternalLink className="size-3.5 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
      </span>
      <span className="mt-1.5 block text-sm font-semibold leading-snug">{path.label}</span>
      <span className="mt-1.5 block text-xs leading-relaxed text-muted-foreground">{path.navigation}</span>
    </a>
  )
}

export function OperatorRunbook({ runbookUrl, previewData, diagrams, onOpenDiagram }: OperatorRunbookProps) {
  const [runbook, setRunbook] = useState<OperatorRunbookPayload | null>(previewData ?? null)
  const [loading, setLoading] = useState(Boolean(runbookUrl && !previewData))
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [categoryId, setCategoryId] = useState('all')
  const [system, setSystem] = useState('all')
  const [selectedId, setSelectedId] = useState('')

  const loadRunbook = useCallback(async () => {
    if (previewData) {
      setRunbook(previewData)
      setLoading(false)
      setError(null)
      setSelectedId((current) => (
        previewData.procedures.some((procedure) => procedure.id === current)
          ? current
          : previewData.procedures[0]?.id ?? ''
      ))
      return
    }
    if (!runbookUrl) {
      setRunbook(null)
      setLoading(false)
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<OperatorRunbookPayload>(runbookUrl)
      setRunbook(data)
      setSelectedId((current) => (
        data.procedures.some((procedure) => procedure.id === current)
          ? current
          : data.procedures[0]?.id ?? ''
      ))
    } catch (e) {
      setRunbook(null)
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [previewData, runbookUrl])

  useEffect(() => {
    void loadRunbook()
  }, [loadRunbook])

  const systems = useMemo(() => (
    [...new Set(runbook?.procedures.flatMap((procedure) => procedure.systems) ?? [])].sort()
  ), [runbook])

  const filteredProcedures = useMemo(() => {
    if (!runbook) return []
    const normalizedQuery = query.trim().toLowerCase()
    return runbook.procedures.filter((procedure) => (
      (categoryId === 'all' || procedure.category === categoryId)
      && (system === 'all' || procedure.systems.includes(system))
      && (!normalizedQuery || searchableText(procedure).includes(normalizedQuery))
    ))
  }, [runbook, query, categoryId, system])

  const selectedProcedure = useMemo(() => (
    filteredProcedures.find((procedure) => procedure.id === selectedId)
    ?? filteredProcedures[0]
    ?? null
  ), [filteredProcedures, selectedId])

  const diagramById = useMemo(() => new Map(diagrams.map((diagram) => [diagram.id, diagram])), [diagrams])

  function selectCategory(nextCategoryId: string) {
    setCategoryId(nextCategoryId)
    setSystem('all')
  }

  function selectSystem(nextSystem: string) {
    setSystem(nextSystem)
    if (nextSystem !== 'all') setCategoryId('all')
  }

  if (loading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center" aria-live="polite">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="size-5 animate-spin motion-reduce:animate-none" />
          Loading Operator Runbook
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        <div className="mx-auto max-w-xl rounded-xl border border-destructive/25 bg-destructive/5 p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 size-5 shrink-0 text-destructive" />
            <div className="min-w-0">
              <h2 className="font-semibold">Could not load Operator Runbook</h2>
              <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{error}</p>
              <Button className="mt-4 min-h-11" variant="outline" onClick={() => void loadRunbook()}>
                <RefreshCw className="size-4" /> Retry
              </Button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if ((!runbookUrl && !previewData) || !runbook) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        <EmptyRunbook message="Publish a schema-v2 architecture bundle to make the task-first runbook available here." />
      </div>
    )
  }

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[17rem_minmax(0,1fr)] lg:overflow-hidden">
      <aside className="border-b bg-muted/15 p-4 lg:overflow-y-auto lg:border-b-0 lg:border-r">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search tasks, systems, steps…"
            aria-label="Search Operator Runbook"
            className="h-11 pl-9"
          />
        </div>

        <div className="mt-5">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <Filter className="size-3.5" /> By task
          </div>
          <nav className="mt-2 grid grid-cols-2 gap-2 lg:grid-cols-1" aria-label="Runbook task categories">
            <button
              type="button"
              onClick={() => selectCategory('all')}
              aria-pressed={categoryId === 'all'}
              className={`min-h-11 rounded-lg border px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                categoryId === 'all' ? 'border-primary/40 bg-primary/10 text-primary' : 'bg-background hover:bg-muted'
              }`}
            >
              <span className="font-medium">All tasks</span>
              <span className="ml-2 text-xs text-muted-foreground">{runbook.procedures.length}</span>
            </button>
            {runbook.categories.map((category) => {
              const count = runbook.procedures.filter((procedure) => procedure.category === category.id).length
              return (
                <button
                  key={category.id}
                  type="button"
                  onClick={() => selectCategory(category.id)}
                  title={category.description}
                  aria-pressed={categoryId === category.id}
                  className={`min-h-11 rounded-lg border px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    categoryId === category.id ? 'border-primary/40 bg-primary/10 text-primary' : 'bg-background hover:bg-muted'
                  }`}
                >
                  <span className="font-medium">{category.label}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{count}</span>
                </button>
              )
            })}
          </nav>
        </div>

        <div className="mt-5 hidden sm:block">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">By system</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => selectSystem('all')}
              aria-pressed={system === 'all'}
              className={`min-h-9 rounded-full border px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                system === 'all' ? 'border-primary/40 bg-primary/10 text-primary' : 'bg-background hover:bg-muted'
              }`}
            >
              All systems
            </button>
            {systems.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => selectSystem(item)}
                aria-pressed={system === item}
                className={`min-h-9 rounded-full border px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  system === item ? 'border-primary/40 bg-primary/10 text-primary' : 'bg-background hover:bg-muted'
                }`}
              >
                {item}
              </button>
            ))}
          </div>
        </div>

        <details className="mt-5 rounded-lg border bg-background sm:hidden">
          <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            Filter by system
            <span className="text-xs font-normal text-muted-foreground">{system === 'all' ? 'All systems' : system}</span>
          </summary>
          <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto border-t p-3">
            <button
              type="button"
              onClick={() => selectSystem('all')}
              aria-pressed={system === 'all'}
              className={`min-h-9 rounded-full border px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                system === 'all' ? 'border-primary/40 bg-primary/10 text-primary' : 'bg-background hover:bg-muted'
              }`}
            >
              All systems
            </button>
            {systems.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => selectSystem(item)}
                aria-pressed={system === item}
                className={`min-h-9 rounded-full border px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  system === item ? 'border-primary/40 bg-primary/10 text-primary' : 'bg-background hover:bg-muted'
                }`}
              >
                {item}
              </button>
            ))}
          </div>
        </details>

        {(query || categoryId !== 'all' || system !== 'all') && (
          <Button
            variant="ghost"
            className="mt-4 min-h-11 w-full justify-start"
            onClick={() => {
              setQuery('')
              setCategoryId('all')
              setSystem('all')
            }}
          >
            <RotateCcw className="size-4" /> Clear filters
          </Button>
        )}
      </aside>

      <main className="min-w-0 p-4 sm:p-6 lg:overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <div className="flex flex-col gap-2 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Read-only operations guide</p>
              <h2 className="mt-1 text-2xl font-semibold tracking-tight">{runbook.title}</h2>
              <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">{runbook.description}</p>
            </div>
            <Badge variant="outline" className="w-fit">{filteredProcedures.length} procedures</Badge>
          </div>

          <section className="mt-5 rounded-xl border border-primary/20 bg-primary/5 p-4">
            <h3 className="flex items-center gap-2 text-sm font-semibold">
              <KeyRound className="size-4 text-primary" /> {runbook.access.title}
            </h3>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{runbook.access.instruction}</p>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{runbook.access.access_note}</p>
          </section>

          {filteredProcedures.length === 0 ? (
            <div className="mt-6">
              <EmptyRunbook title="No matching procedures" message="No procedure matches this search. Clear the search or use a different word." />
            </div>
          ) : (
            <div className="mt-6 grid gap-6 xl:grid-cols-[19rem_minmax(0,1fr)]">
              <nav className="space-y-2" aria-label="Matching runbook procedures">
                {filteredProcedures.map((procedure) => {
                  const risk = riskPresentation[procedure.risk]
                  const RiskIcon = risk.icon
                  const active = selectedProcedure?.id === procedure.id
                  return (
                    <button
                      key={procedure.id}
                      type="button"
                      onClick={() => setSelectedId(procedure.id)}
                      className={`w-full rounded-xl border border-l-4 p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${risk.accent} ${
                        active ? 'border-y-primary/30 border-r-primary/30 bg-primary/5' : 'bg-background hover:bg-muted/60'
                      }`}
                    >
                      <span className="flex items-start justify-between gap-3">
                        <span className="min-w-0">
                          <span className="block text-sm font-semibold leading-snug">{procedure.title}</span>
                          <span className="mt-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                            <RiskIcon className="size-3.5" /> {risk.label}
                          </span>
                        </span>
                        <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                      </span>
                    </button>
                  )
                })}
              </nav>

              {selectedProcedure && (() => {
                const risk = riskPresentation[selectedProcedure.risk]
                const RiskIcon = risk.icon
                return (
                  <article className="min-w-0 rounded-2xl border bg-card shadow-xs">
                    <header className={`border-b border-l-4 p-5 sm:p-6 ${risk.accent}`}>
                      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge variant="outline" className={risk.badge}>
                              <RiskIcon className="size-3.5" /> {risk.label}
                            </Badge>
                            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                              <Clock3 className="size-3.5" /> {selectedProcedure.estimated_time}
                            </span>
                          </div>
                          <h2 className="mt-3 text-xl font-semibold tracking-tight sm:text-2xl">{selectedProcedure.title}</h2>
                          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">{selectedProcedure.summary}</p>
                        </div>
                      </div>
                      <div className="mt-4 flex flex-wrap gap-1.5">
                        {selectedProcedure.systems.map((item) => <Badge key={item} variant="secondary">{item}</Badge>)}
                      </div>
                    </header>

                    <div className="space-y-6 p-5 sm:p-6">
                      <SectionList title="Before you start" icon={Info} items={selectedProcedure.prerequisites} />

                      <section>
                        <h3 className="flex items-center gap-2 text-sm font-semibold">
                          <ArrowRight className="size-4 text-primary" /> Procedure
                        </h3>
                        <ol className="mt-4 space-y-3">
                          {selectedProcedure.steps.map((step, index) => {
                            const hasBrowserPath = Boolean(step.dashboard_path || step.manual_path)
                            return (
                              <li key={`${step.title}-${index}`} className="grid grid-cols-[2rem_minmax(0,1fr)] gap-3">
                                <span className="flex size-8 items-center justify-center rounded-full border bg-muted text-sm font-semibold tabular-nums">{index + 1}</span>
                                <div className="rounded-xl border bg-background p-3.5">
                                  <h4 className="text-sm font-semibold">{step.title}</h4>
                                  <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{step.detail}</p>
                                  {hasBrowserPath && (
                                    <div className="mt-3 grid gap-2 sm:grid-cols-2">
                                      {step.dashboard_path && <BrowserPathCard path={step.dashboard_path} variant="dashboard" />}
                                      {step.manual_path && <BrowserPathCard path={step.manual_path} variant="manual" />}
                                    </div>
                                  )}
                                </div>
                              </li>
                            )
                          })}
                        </ol>
                      </section>

                      <div className="grid gap-4 md:grid-cols-2">
                        <SectionList title="Verify" icon={CheckCircle2} items={selectedProcedure.verify} tone="success" />
                        <SectionList title="Stop conditions" icon={StopCircle} items={selectedProcedure.stop_conditions} tone="danger" />
                      </div>
                      <SectionList title="Rollback / recovery" icon={RotateCcw} items={selectedProcedure.rollback} />

                      {selectedProcedure.portals.length > 0 && (
                        <section>
                          <h3 className="flex items-center gap-2 text-sm font-semibold">
                            <ExternalLink className="size-4 text-muted-foreground" /> Where to go
                          </h3>
                          <div className="mt-3 grid gap-2 sm:grid-cols-2">
                            {selectedProcedure.portals.map((portal) => (
                              <a
                                key={`${portal.label}-${portal.url}`}
                                href={portal.url}
                                target="_blank"
                                rel="noreferrer"
                                className="group rounded-xl border bg-muted/15 p-3 transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              >
                                <span className="flex items-center justify-between gap-3 text-sm font-semibold">
                                  {portal.label} <ExternalLink className="size-3.5 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
                                </span>
                                <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">{portal.location}</span>
                              </a>
                            ))}
                          </div>
                        </section>
                      )}

                      {selectedProcedure.related_diagrams.length > 0 && (
                        <section className="border-t pt-5">
                          <h3 className="flex items-center gap-2 text-sm font-semibold">
                            <MapIcon className="size-4 text-muted-foreground" /> Related system maps
                          </h3>
                          <div className="mt-3 flex flex-wrap gap-2">
                            {selectedProcedure.related_diagrams.map((diagramId) => {
                              const diagram = diagramById.get(diagramId)
                              if (!diagram) return null
                              return (
                                <Button key={diagramId} variant="outline" className="min-h-11" onClick={() => onOpenDiagram(diagramId)}>
                                  {diagram.title} <ArrowRight className="size-4" />
                                </Button>
                              )
                            })}
                          </div>
                        </section>
                      )}
                    </div>
                  </article>
                )
              })()}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
