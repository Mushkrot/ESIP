import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { MessageSquarePlus, Loader2, RefreshCw } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { PageScroll } from '@/components/PageScroll'
import { useAuth } from '@/contexts/AuthContext'
import type { FeedbackEntry, FeedbackReply } from '@/types/api'

type FeedbackEntryAdmin = FeedbackEntry & { uid: string }

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

export function AdminFeedbackPage() {
  const { loading: authLoading, role } = useAuth()
  const [entries, setEntries] = useState<FeedbackEntryAdmin[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [replyDrafts, setReplyDrafts] = useState<Record<string, string>>({})
  const [replying, setReplying] = useState<Record<string, boolean>>({})
  const [replyErrors, setReplyErrors] = useState<Record<string, string>>({})

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<FeedbackEntryAdmin[]>('/api/feedback/all')
      setEntries(data)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  async function sendReply(entry: FeedbackEntryAdmin) {
    const key = entry.id
    const text = (replyDrafts[key] ?? '').trim()
    if (!text) {
      setReplyErrors(prev => ({ ...prev, [key]: 'Reply text cannot be empty.' }))
      return
    }
    setReplyErrors(prev => ({ ...prev, [key]: '' }))
    setReplying(prev => ({ ...prev, [key]: true }))
    try {
      const created = await apiFetch<FeedbackReply>(
        `/api/feedback/${entry.uid}/${entry.id}/reply`,
        {
          method: 'POST',
          body: JSON.stringify({ text }),
        },
      )
      setEntries(prev => prev.map(e => (
        e.id === entry.id ? { ...e, replies: [...(e.replies ?? []), created] } : e
      )))
      setReplyDrafts(prev => ({ ...prev, [key]: '' }))
    } catch (e) {
      setReplyErrors(prev => ({ ...prev, [key]: (e as Error).message }))
    } finally {
      setReplying(prev => ({ ...prev, [key]: false }))
    }
  }

  useEffect(() => {
    if (role !== 'super_admin') return
    void load()
  }, [role])

  if (authLoading) {
    return (
      <div className="min-h-[40vh] flex items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (role !== 'super_admin') {
    return <Navigate to="/" replace />
  }

  return (
    <PageScroll>
      <div className="p-6 max-w-3xl mr-auto space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <MessageSquarePlus className="size-5 text-primary" />
          <h1 className="text-lg font-semibold">Beta Feedback</h1>
          {!loading && (
            <span className="text-xs text-muted-foreground bg-muted px-2 py-0.5 rounded-full">
              {entries.length} {entries.length === 1 ? 'entry' : 'entries'}
            </span>
          )}
        </div>
        <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}
          className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground">
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

      {!loading && !error && entries.length === 0 && (
        <p className="text-center text-sm text-muted-foreground py-12">No feedback submitted yet.</p>
      )}

      {!loading && entries.map(e => (
        <div key={e.id} className="rounded-lg border bg-card p-4 space-y-2">
          <div className="flex items-start justify-between gap-4">
            <div className="text-sm font-medium">{e.user_name}</div>
            <div className="text-xs text-muted-foreground whitespace-nowrap shrink-0">
              {formatDate(e.created_at)}
            </div>
          </div>
          <div className="text-xs text-muted-foreground">{e.user_email}</div>
          <p className="text-sm leading-relaxed whitespace-pre-wrap">{e.text}</p>

          {(e.replies ?? []).length > 0 && (
            <div className="pt-2 mt-2 border-t space-y-2">
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">Replies</p>
              {(e.replies ?? []).map((r) => (
                <div key={r.id} className="rounded border bg-muted/40 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium">{r.author_name}</span>
                    <span className="text-xs text-muted-foreground">{formatDate(r.created_at)}</span>
                  </div>
                  <p className="text-sm whitespace-pre-wrap mt-1">{r.text}</p>
                </div>
              ))}
            </div>
          )}

          <div className="pt-2 mt-2 border-t space-y-2">
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">Reply as admin</p>
            <textarea
              value={replyDrafts[e.id] ?? ''}
              onChange={(ev) => setReplyDrafts(prev => ({ ...prev, [e.id]: ev.target.value }))}
              rows={3}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm
                         ring-offset-background placeholder:text-muted-foreground
                         focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring
                         focus-visible:ring-offset-2 resize-y min-h-[84px]"
              placeholder="Write your reply to this feedback..."
              disabled={Boolean(replying[e.id])}
            />
            {replyErrors[e.id] && (
              <p className="text-xs text-destructive">{replyErrors[e.id]}</p>
            )}
            <Button
              size="sm"
              onClick={() => void sendReply(e)}
              disabled={Boolean(replying[e.id]) || !(replyDrafts[e.id] ?? '').trim()}
            >
              {replying[e.id] ? 'Sending...' : 'Send reply'}
            </Button>
          </div>
        </div>
      ))}
      </div>
    </PageScroll>
  )
}
