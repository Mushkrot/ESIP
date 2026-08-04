import { useCallback, useEffect, useMemo, useState } from 'react'
import { Bell, CheckCircle2, Loader2, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { apiFetch } from '@/lib/api'
import { Button } from '@/components/ui/button'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import { useAuth } from '@/contexts/AuthContext'

interface AnnouncementReadState {
  dismissed_at?: string | null
}

interface Announcement {
  id: string
  title: string
  body_markdown: string
  severity: 'info' | 'important' | 'breaking'
  published_at: string
  active_from: string
  read_state?: AnnouncementReadState | null
}

function severityLabel(value: string) {
  if (value === 'breaking') return 'Breaking change'
  if (value === 'important') return 'Important'
  return 'Update'
}

function formatDate(value: string | null | undefined) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

const SNOOZE_STORAGE_KEY = 'esip.whatsNew.snoozes.v1'
const SNOOZE_TTL_MS = 12 * 60 * 60 * 1000

type SnoozeMap = Record<string, number>

function snoozeKey(email: string, announcementId: string) {
  const owner = email.trim().toLowerCase() || 'anonymous'
  return `${owner}::${announcementId}`
}

function writeSnoozes(snoozes: SnoozeMap) {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(SNOOZE_STORAGE_KEY, JSON.stringify(snoozes))
  } catch {
    // sessionStorage can be unavailable in privacy-restricted contexts.
  }
}

function readSnoozes(now = Date.now()): SnoozeMap {
  if (typeof window === 'undefined') return {}
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(SNOOZE_STORAGE_KEY) || '{}') as SnoozeMap
    const active = Object.fromEntries(
      Object.entries(parsed).filter(([, snoozedUntil]) => Number(snoozedUntil) > now),
    )
    if (Object.keys(active).length !== Object.keys(parsed).length) writeSnoozes(active)
    return active
  } catch {
    return {}
  }
}

function isSnoozed(snoozes: SnoozeMap, email: string, announcementId: string, now = Date.now()) {
  return (snoozes[snoozeKey(email, announcementId)] ?? 0) > now
}

export function WhatsNewCenter() {
  const { rolePreview, user } = useAuth()
  const [active, setActive] = useState<Announcement[]>([])
  const [history, setHistory] = useState<Announcement[]>([])
  const [snoozes, setSnoozes] = useState<SnoozeMap>(() => readSnoozes())
  const [open, setOpen] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [dismissingId, setDismissingId] = useState<string | null>(null)
  const userEmail = user?.email ?? ''

  const visibleActive = useMemo(
    () => {
      const now = Date.now()
      return active.filter(item => !isSnoozed(snoozes, userEmail, item.id, now))
    },
    [active, snoozes, userEmail],
  )
  const unread = visibleActive[0] ?? null

  const loadActive = useCallback(async () => {
    try {
      const data = await apiFetch<Announcement[]>('/api/announcements/active?surface=web')
      setActive(data)
    } catch {
      setActive([])
    }
  }, [])

  const loadHistory = useCallback(async () => {
    setLoadingHistory(true)
    try {
      const data = await apiFetch<Announcement[]>('/api/announcements/history?surface=web')
      setHistory(data)
    } catch {
      setHistory([])
    } finally {
      setLoadingHistory(false)
    }
  }, [])

  const snooze = useCallback((announcementId: string) => {
    setSnoozes(() => {
      const next = {
        ...readSnoozes(),
        [snoozeKey(userEmail, announcementId)]: Date.now() + SNOOZE_TTL_MS,
      }
      writeSnoozes(next)
      return next
    })
    setActive(prev => prev.filter(item => item.id !== announcementId))
  }, [userEmail])

  const dismiss = useCallback(async (announcementId: string) => {
    if (rolePreview) {
      snooze(announcementId)
      return
    }

    setDismissingId(announcementId)
    setActive(prev => prev.filter(item => item.id !== announcementId))
    setHistory(prev => prev.map(item => (
      item.id === announcementId
        ? { ...item, read_state: { ...(item.read_state ?? {}), dismissed_at: new Date().toISOString() } }
        : item
    )))
    try {
      await apiFetch(`/api/announcements/${announcementId}/dismiss`, { method: 'POST' })
    } catch {
      snooze(announcementId)
    } finally {
      setDismissingId(null)
    }
  }, [rolePreview, snooze])

  useEffect(() => {
    void loadActive()
    const onVisible = () => {
      if (document.visibilityState === 'visible') void loadActive()
    }
    const onOpen = () => setOpen(true)
    const interval = window.setInterval(() => void loadActive(), 15 * 60 * 1000)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('esip:open-whats-new', onOpen)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('esip:open-whats-new', onOpen)
      window.clearInterval(interval)
    }
  }, [loadActive])

  useEffect(() => {
    if (open) void loadHistory()
  }, [open, loadHistory])

  const list = useMemo(() => {
    const byId = new Map<string, Announcement>()
    for (const item of [...active, ...history]) byId.set(item.id, item)
    return Array.from(byId.values())
      .sort((a, b) => new Date(b.published_at).getTime() - new Date(a.published_at).getTime())
  }, [active, history])

  return (
    <>
      {unread && (
        <div className="fixed right-4 bottom-4 z-40 w-[min(24rem,calc(100vw-2rem))] rounded-lg border bg-card p-3 shadow-lg">
          <div className="flex items-start gap-3">
            <Bell className="mt-0.5 size-4 shrink-0 text-primary" />
            <div className="min-w-0 flex-1 space-y-2">
              <div className="flex items-center gap-2">
                <p className="truncate text-sm font-medium">What’s new in ESIP</p>
                <Badge variant="outline" className="h-5 text-[11px]">
                  {severityLabel(unread.severity)}
                </Badge>
              </div>
              <p className="text-sm font-medium leading-snug">{unread.title}</p>
              <div className="flex gap-2">
                <Button size="sm" onClick={() => setOpen(true)}>
                  View details
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={dismissingId === unread.id}
                  onClick={() => void dismiss(unread.id)}
                >
                  {dismissingId === unread.id
                    ? <Loader2 className="size-3.5 animate-spin" />
                    : <CheckCircle2 className="size-3.5" />}
                  Got it
                </Button>
              </div>
            </div>
            <Button
              size="icon"
              variant="ghost"
              className="size-7 shrink-0"
              onClick={() => snooze(unread.id)}
              title="Hide for now"
            >
              <X className="size-4" />
            </Button>
          </div>
        </div>
      )}

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent className="w-[min(34rem,100vw)] sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>What’s New</SheetTitle>
            <SheetDescription>Recent ESIP changes published by the admin team.</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
            {loadingHistory && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" /> Loading changes…
              </div>
            )}
            {!loadingHistory && list.length === 0 && (
              <p className="text-sm text-muted-foreground">No published updates yet.</p>
            )}
            <div className="space-y-3">
              {list.map(item => (
                <article key={item.id} className="rounded-lg border bg-card p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-semibold">{item.title}</h3>
                    <Badge variant={item.read_state?.dismissed_at ? 'secondary' : 'outline'} className="h-5 text-[11px]">
                      {item.read_state?.dismissed_at ? 'Read' : severityLabel(item.severity)}
                    </Badge>
                  </div>
                  <div className="prose prose-sm max-w-none dark:prose-invert">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.body_markdown}</ReactMarkdown>
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-3">
                    <span className="text-xs text-muted-foreground">{formatDate(item.published_at)}</span>
                    {!item.read_state?.dismissed_at && (
                      <Button size="sm" variant="outline" onClick={() => void dismiss(item.id)}>
                        Got it
                      </Button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </>
  )
}
