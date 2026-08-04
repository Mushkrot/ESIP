import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  ExternalLink,
  KeyRound,
  Loader2,
  Map as MapIcon,
  Network,
  RefreshCw,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

interface Portal {
  label: string
  location: string
  url: string
}

interface OverviewSystem {
  id: string
  name: string
  purpose: string
  connects_to: string[]
  portal: Portal
  map_id?: string
}

interface OverviewGroup {
  id: string
  title: string
  description: string
  systems: OverviewSystem[]
}

interface OverviewProcess {
  id: string
  title: string
  description: string
  system_ids: string[]
}

export interface PlatformOverviewPayload {
  schema_version: number
  title: string
  description: string
  access: {
    title: string
    instruction: string
    access_note: string
  }
  processes: OverviewProcess[]
  groups: OverviewGroup[]
}

interface PlatformOverviewProps {
  overviewUrl?: string
  onOpenDiagram: (diagramId: string) => void
}

function EmptyOverview({ message }: { message: string }) {
  return (
    <div className="flex min-h-72 flex-col items-center justify-center rounded-xl border border-dashed bg-muted/15 px-6 text-center">
      <Network className="size-8 text-muted-foreground" />
      <h2 className="mt-4 text-sm font-semibold">Platform Overview unavailable</h2>
      <p className="mt-1 max-w-md text-sm leading-relaxed text-muted-foreground">{message}</p>
    </div>
  )
}

export function PlatformOverview({ overviewUrl, onOpenDiagram }: PlatformOverviewProps) {
  const [overview, setOverview] = useState<PlatformOverviewPayload | null>(null)
  const [loading, setLoading] = useState(Boolean(overviewUrl))
  const [error, setError] = useState<string | null>(null)

  const loadOverview = useCallback(async () => {
    if (!overviewUrl) {
      setOverview(null)
      setError(null)
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    try {
      setOverview(await apiFetch<PlatformOverviewPayload>(overviewUrl))
    } catch (e) {
      setOverview(null)
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [overviewUrl])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  const systemsById = useMemo(() => {
    const entries = overview?.groups.flatMap((group) => group.systems) ?? []
    return new Map(entries.map((system) => [system.id, system]))
  }, [overview])

  if (loading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center" aria-live="polite">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="size-5 animate-spin motion-reduce:animate-none" /> Loading Platform Overview
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
              <h2 className="font-semibold">Could not load Platform Overview</h2>
              <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{error}</p>
              <Button className="mt-4 min-h-11" variant="outline" onClick={() => void loadOverview()}>
                <RefreshCw className="size-4" /> Retry
              </Button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (!overview) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        <div className="mx-auto max-w-7xl">
          <EmptyOverview message="Publish an Architecture bundle with the Platform Overview to make the high-level guide available here." />
        </div>
      </div>
    )
  }

  return (
    <main className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto max-w-7xl pb-8">
        <header className="border-b pb-5">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">At-a-glance system guide</p>
          <h2 className="mt-1 text-2xl font-semibold tracking-tight">{overview.title}</h2>
          <p className="mt-2 max-w-4xl text-sm leading-relaxed text-muted-foreground">{overview.description}</p>
        </header>

        <section className="mt-5 rounded-xl border border-primary/20 bg-primary/5 p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <KeyRound className="size-4 text-primary" /> {overview.access.title}
          </h3>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{overview.access.instruction}</p>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{overview.access.access_note}</p>
        </section>

        <section className="mt-7" aria-labelledby="overview-processes">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">How the platform works</p>
              <h3 id="overview-processes" className="mt-1 text-lg font-semibold">Four important flows</h3>
            </div>
            <Badge variant="outline">Read left to right</Badge>
          </div>
          <div className="mt-4 grid gap-3 xl:grid-cols-2">
            {overview.processes.map((process) => (
              <article key={process.id} className="rounded-xl border bg-card p-4 shadow-xs">
                <h4 className="text-sm font-semibold">{process.title}</h4>
                <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{process.description}</p>
                <div className="mt-4 flex flex-wrap items-center gap-1.5" aria-label={`${process.title} systems`}>
                  {process.system_ids.map((systemId, index) => {
                    const system = systemsById.get(systemId)
                    if (!system) return null
                    return (
                      <div key={systemId} className="flex items-center gap-1.5">
                        {index > 0 && <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />}
                        <span className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium">{system.name}</span>
                      </div>
                    )
                  })}
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="mt-8" aria-labelledby="overview-systems">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">System directory</p>
            <h3 id="overview-systems" className="mt-1 text-lg font-semibold">What each system is for, and where to open it</h3>
            <p className="mt-1 text-sm text-muted-foreground">Each card links directly to the relevant browser portal. Use the related System Map only when you need the technical detail behind that card.</p>
          </div>

          <div className="mt-5 space-y-8">
            {overview.groups.map((group) => (
              <section key={group.id} aria-labelledby={`group-${group.id}`}>
                <div className="border-b pb-3">
                  <h4 id={`group-${group.id}`} className="text-base font-semibold">{group.title}</h4>
                  <p className="mt-1 text-sm text-muted-foreground">{group.description}</p>
                </div>
                <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {group.systems.map((system) => {
                    const mapId = system.map_id
                    return (
                    <article key={system.id} className="flex min-w-0 flex-col rounded-xl border bg-card p-4 shadow-xs">
                      <h5 className="text-sm font-semibold">{system.name}</h5>
                      <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{system.purpose}</p>
                      <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                        <span className="font-semibold text-foreground">Connects to: </span>
                        {system.connects_to.map((id) => systemsById.get(id)?.name ?? id).join(' · ')}
                      </p>
                      <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
                        <a
                          href={system.portal.url}
                          target="_blank"
                          rel="noreferrer"
                          className="group rounded-lg border bg-muted/15 p-3 transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <span className="flex items-center justify-between gap-2 text-sm font-semibold">
                            {system.portal.label} <ExternalLink className="size-3.5 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
                          </span>
                          <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">{system.portal.location}</span>
                        </a>
                        {mapId && (
                          <Button variant="outline" className="min-h-11 justify-start" onClick={() => onOpenDiagram(mapId)}>
                            <MapIcon className="size-4" /> Open related System Map
                          </Button>
                        )}
                      </div>
                    </article>
                    )
                  })}
                </div>
              </section>
            ))}
          </div>
        </section>
      </div>
    </main>
  )
}
