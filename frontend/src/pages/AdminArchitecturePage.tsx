import { useEffect, useMemo, useRef, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { BookOpenCheck, Map, Maximize2, Minus, Network, Plus, RefreshCw, RotateCcw, Loader2 } from 'lucide-react'
import { apiFetch, authHeaders } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useAuth } from '@/contexts/AuthContext'
import { OperatorRunbook } from '@/components/admin/OperatorRunbook'
import { PlatformOverview } from '@/components/admin/PlatformOverview'

interface ArchitectureDiagram {
  id: string
  title: string
  asset: string
  asset_url: string
  kind?: string
  source?: string
  description?: string
}

interface ArchitectureManifest {
  title: string
  version: string
  source_tool?: string
  d2_layout?: string
  source_commit?: string
  generated_at?: string
  diagrams: ArchitectureDiagram[]
  runbook?: {
    title: string
    description?: string
    url: string
    procedure_count?: number
  } | null
  overview?: {
    title: string
    description?: string
    url: string
    system_count?: number
  } | null
}

interface ViewState {
  scale: number
  x: number
  y: number
}

const INITIAL_VIEW: ViewState = { scale: 0.72, x: 0, y: 0 }

function formatDate(iso?: string): string {
  if (!iso) return 'Not published'
  return new Date(iso).toLocaleString('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function clampScale(value: number): number {
  return Math.min(3, Math.max(0.18, value))
}

async function fetchAssetObjectUrl(assetUrl: string): Promise<string> {
  const res = await fetch(assetUrl, {
    headers: await authHeaders(),
    cache: 'no-store',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`)
  }
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}

export function AdminArchitecturePage() {
  const { loading: authLoading, isAdmin } = useAuth()
  const [manifest, setManifest] = useState<ArchitectureManifest | null>(null)
  const [surface, setSurface] = useState<'overview' | 'map' | 'runbook'>('map')
  const [selectedId, setSelectedId] = useState<string>('')
  const [svgUrl, setSvgUrl] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [assetLoading, setAssetLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<ViewState>(INITIAL_VIEW)
  const [dragging, setDragging] = useState(false)
  const dragStart = useRef<{ pointerId: number; x: number; y: number; viewX: number; viewY: number } | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)

  const selectedDiagram = useMemo(
    () => manifest?.diagrams.find((diagram) => diagram.id === selectedId) ?? manifest?.diagrams[0] ?? null,
    [manifest, selectedId],
  )
  const selectedDiagramAssetUrl = selectedDiagram?.asset_url

  async function loadManifest() {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<ArchitectureManifest>('/api/admin/architecture/manifest')
      setManifest(data)
      setSelectedId((current) => (
        data.diagrams.some((diagram) => diagram.id === current)
          ? current
          : data.diagrams[0]?.id ?? ''
      ))
      setSurface((current) => (current === 'map' && data.overview ? 'overview' : current))
    } catch (e) {
      setManifest(null)
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!isAdmin) return
    void loadManifest()
  }, [isAdmin])

  useEffect(() => {
    if (!selectedDiagramAssetUrl) {
      setSvgUrl(null)
      return
    }
    let cancelled = false
    let objectUrl: string | null = null
    setAssetLoading(true)
    setError(null)
    fetchAssetObjectUrl(selectedDiagramAssetUrl)
      .then((url) => {
        objectUrl = url
        if (!cancelled) {
          setSvgUrl(url)
          setView(INITIAL_VIEW)
        }
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message)
      })
      .finally(() => {
        if (!cancelled) setAssetLoading(false)
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [selectedDiagramAssetUrl])

  useEffect(() => {
    return () => {
      if (svgUrl) URL.revokeObjectURL(svgUrl)
    }
  }, [svgUrl])

  function zoom(delta: number) {
    setView((prev) => ({ ...prev, scale: clampScale(prev.scale + delta) }))
  }

  function fit() {
    setView(INITIAL_VIEW)
  }

  function onWheel(e: React.WheelEvent<HTMLDivElement>) {
    e.preventDefault()
    const next = clampScale(view.scale + (e.deltaY > 0 ? -0.08 : 0.08))
    setView((prev) => ({ ...prev, scale: next }))
  }

  function onPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    const target = e.currentTarget
    target.setPointerCapture(e.pointerId)
    setDragging(true)
    dragStart.current = {
      pointerId: e.pointerId,
      x: e.clientX,
      y: e.clientY,
      viewX: view.x,
      viewY: view.y,
    }
  }

  function onPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const start = dragStart.current
    if (!start || start.pointerId !== e.pointerId) return
    setView((prev) => ({
      ...prev,
      x: start.viewX + e.clientX - start.x,
      y: start.viewY + e.clientY - start.y,
    }))
  }

  function onPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    if (dragStart.current?.pointerId === e.pointerId) {
      dragStart.current = null
      setDragging(false)
    }
  }

  async function openFullscreen() {
    if (!stageRef.current) return
    if (document.fullscreenElement) {
      await document.exitFullscreen()
      return
    }
    await stageRef.current.requestFullscreen()
  }

  function openDiagram(diagramId: string) {
    setSelectedId(diagramId)
    setSurface('map')
  }

  if (authLoading) {
    return (
      <div className="min-h-[40vh] flex items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (!isAdmin) return <Navigate to="/" replace />

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <header className="flex shrink-0 flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold leading-tight">Architecture</h1>
          <p className="mt-0.5 text-xs text-muted-foreground">Current system maps and read-only operating guidance</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="grid min-h-11 flex-1 grid-cols-3 rounded-lg bg-muted p-1 sm:flex-none" role="tablist" aria-label="Architecture views">
            <button
              type="button"
              role="tab"
              aria-selected={surface === 'overview'}
              onClick={() => setSurface('overview')}
              className={`flex min-h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                surface === 'overview' ? 'bg-background text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <Network className="size-4" /> <span className="hidden sm:inline">Platform Overview</span><span className="sm:hidden">Overview</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={surface === 'map'}
              onClick={() => setSurface('map')}
              className={`flex min-h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                surface === 'map' ? 'bg-background text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <Map className="size-4" /> System Map
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={surface === 'runbook'}
              onClick={() => setSurface('runbook')}
              className={`flex min-h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                surface === 'runbook' ? 'bg-background text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <BookOpenCheck className="size-4" /> Operator Runbook
            </button>
          </div>
          <Button variant="outline" size="icon" onClick={() => void loadManifest()} disabled={loading} title="Refresh published architecture">
            {loading ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" /> : <RefreshCw className="size-4" />}
          </Button>
        </div>
      </header>

      {surface === 'overview' ? (
        <PlatformOverview
          overviewUrl={manifest?.overview?.url}
          onOpenDiagram={openDiagram}
        />
      ) : surface === 'runbook' ? (
        <OperatorRunbook
          runbookUrl={manifest?.runbook?.url}
          diagrams={manifest?.diagrams ?? []}
          onOpenDiagram={openDiagram}
        />
      ) : (
      <div className="flex min-h-0 flex-1">
        <aside className="w-72 shrink-0 border-r bg-muted/20 p-4 overflow-y-auto">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Published bundle</p>

          {manifest && (
            <div className="mt-4 space-y-2 text-xs text-muted-foreground">
              <div className="flex flex-wrap gap-1.5">
                <Badge variant="secondary">{manifest.source_tool ?? 'static'}</Badge>
                {manifest.d2_layout && <Badge variant="outline">{manifest.d2_layout}</Badge>}
              </div>
              <p className="break-all">Version: {manifest.version}</p>
              <p>Published: {formatDate(manifest.generated_at)}</p>
              {manifest.source_commit && <p>Commit: {manifest.source_commit}</p>}
            </div>
          )}

          <div className="mt-5 space-y-2">
            <p className="text-xs font-medium uppercase text-muted-foreground">Diagrams</p>
            {loading && (
              <div className="flex items-center gap-2 rounded-md border bg-background p-3 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" /> Loading
              </div>
            )}
            {!loading && manifest?.diagrams.map((diagram) => (
              <button
                key={diagram.id}
                type="button"
                onClick={() => setSelectedId(diagram.id)}
                className={`w-full rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                  selectedDiagram?.id === diagram.id
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'bg-background hover:bg-muted/70'
                }`}
              >
                <span className="block font-medium leading-snug">{diagram.title}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">{diagram.kind ?? 'diagram'}</span>
                {diagram.description && (
                  <span className="mt-1 block text-xs leading-snug text-muted-foreground">
                    {diagram.description}
                  </span>
                )}
              </button>
            ))}
          </div>
        </aside>

        <section className="flex min-w-0 flex-1 flex-col">
          <header className="flex shrink-0 items-center justify-between gap-3 border-b px-4 py-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <Network className="size-5 text-primary" />
                <h2 className="truncate text-base font-semibold">
                  {selectedDiagram?.title ?? 'Architecture diagrams'}
                </h2>
              </div>
              {selectedDiagram?.source && (
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {selectedDiagram.source}
                </p>
              )}
              {selectedDiagram?.description && (
                <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
                  {selectedDiagram.description}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Button variant="outline" size="icon" title="Zoom out" onClick={() => zoom(-0.12)}>
                <Minus className="size-4" />
              </Button>
              <div className="w-14 text-center text-xs tabular-nums text-muted-foreground">
                {Math.round(view.scale * 100)}%
              </div>
              <Button variant="outline" size="icon" title="Zoom in" onClick={() => zoom(0.12)}>
                <Plus className="size-4" />
              </Button>
              <Button variant="outline" size="icon" title="Fit" onClick={fit}>
                <RotateCcw className="size-4" />
              </Button>
              <Button variant="outline" size="icon" title="Fullscreen" onClick={() => void openFullscreen()}>
                <Maximize2 className="size-4" />
              </Button>
            </div>
          </header>

          <div
            ref={stageRef}
            className="relative min-h-0 flex-1 overflow-hidden bg-[#f5f7fb]"
          >
            {error && (
              <div className="absolute left-4 top-4 z-10 max-w-md rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
                {error}
              </div>
            )}
            {assetLoading && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/50">
                <Loader2 className="size-6 animate-spin text-muted-foreground" />
              </div>
            )}
            {!loading && !manifest && !error && (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                Architecture documentation is not published.
              </div>
            )}
            {svgUrl && (
              <div
                className={`h-full w-full touch-none select-none overflow-hidden ${dragging ? 'cursor-grabbing' : 'cursor-grab'}`}
                onWheel={onWheel}
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerCancel={onPointerUp}
              >
                <div
                  className="flex h-full w-full items-center justify-center p-8"
                  style={{
                    transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
                    transformOrigin: 'center center',
                  }}
                >
                  <img
                    src={svgUrl}
                    alt={selectedDiagram?.title ?? 'Architecture diagram'}
                    draggable={false}
                    className="max-h-none max-w-none rounded-sm bg-white shadow-sm"
                  />
                </div>
              </div>
            )}
          </div>
        </section>
      </div>
      )}
    </div>
  )
}
