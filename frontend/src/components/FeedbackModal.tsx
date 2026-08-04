import { useEffect, useRef, useState } from 'react'
import { Pencil, X } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import type { FeedbackEntry, FeedbackInboxMessage } from '@/types/api'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

interface FeedbackModalProps {
  open: boolean
  onClose: () => void
  inbox: FeedbackInboxMessage[]
  onInboxChange: (items: FeedbackInboxMessage[]) => void
}

export function FeedbackModal({ open, onClose, inbox, onInboxChange }: FeedbackModalProps) {
  const [text, setText] = useState('')
  const [editingEntry, setEditingEntry] = useState<FeedbackEntry | null>(null)
  const [entries, setEntries] = useState<FeedbackEntry[]>([])
  const [loadingEntries, setLoadingEntries] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Load previous entries when the modal opens
  useEffect(() => {
    if (!open) return
    setLoadingEntries(true)
    Promise.all([
      apiFetch<FeedbackEntry[]>('/api/feedback').catch(() => []),
      apiFetch<FeedbackInboxMessage[]>('/api/feedback/inbox').catch(() => []),
    ])
      .then(([fEntries, inboxItems]) => {
        setEntries(fEntries)
        onInboxChange(inboxItems)
      })
      .finally(() => setLoadingEntries(false))
  }, [open, onInboxChange])

  // Focus textarea after modal opens
  useEffect(() => {
    if (open) {
      setTimeout(() => textareaRef.current?.focus(), 100)
    }
  }, [open])

  function handleClose() {
    setText('')
    setEditingEntry(null)
    setError(null)
    onClose()
  }

  async function markInboxRead() {
    if (inbox.length === 0) return
    try {
      await apiFetch<{ updated: number }>('/api/feedback/inbox/read', {
        method: 'POST',
        body: JSON.stringify({ message_ids: inbox.map(i => i.id) }),
      })
      onInboxChange([])
    } catch {
      // non-critical: user still sees notifications and history
    }
  }

  function startEdit(entry: FeedbackEntry) {
    setEditingEntry(entry)
    setText(entry.text)
    setError(null)
    textareaRef.current?.focus()
  }

  function cancelEdit() {
    setEditingEntry(null)
    setText('')
    setError(null)
  }

  async function handleSubmit() {
    const trimmed = text.trim()
    if (!trimmed) {
      setError('Please write something before submitting.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      if (editingEntry) {
        // Update existing entry
        const updated = await apiFetch<FeedbackEntry>(
          `/api/feedback/${editingEntry.id}`,
          { method: 'PUT', body: JSON.stringify({ text: trimmed }) },
        )
        setEntries((prev) => prev.map((e) => (e.id === updated.id ? updated : e)))
        setEditingEntry(null)
      } else {
        // Submit new entry
        const created = await apiFetch<FeedbackEntry>('/api/feedback', {
          method: 'POST',
          body: JSON.stringify({ text: trimmed }),
        })
        setEntries((prev) => [created, ...prev])
      }
      setText('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  function formatDate(iso: string) {
    return new Date(iso).toLocaleDateString('en-CA', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  }

  return (
    <Dialog open={open} onOpenChange={(isOpen) => { if (!isOpen) handleClose() }}>
      <DialogContent
        className="flex flex-col gap-0 p-0 overflow-hidden"
        style={{
          resize: 'both',
          width: '540px',
          minWidth: '380px',
          minHeight: '420px',
          maxWidth: '90vw',
          maxHeight: '90vh',
        }}
      >
        <DialogHeader className="px-6 pt-6 pb-4 shrink-0">
          <DialogTitle className="text-lg font-semibold">Share Your Feedback</DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground leading-relaxed">
            We&apos;re testing the AI Assistant and your input directly shapes what we build next.
            Please share:
          </DialogDescription>
          <ul className="mt-2 space-y-0.5 text-sm text-muted-foreground list-none">
            <li>• What features would you like to see added?</li>
            <li>• What&apos;s currently inconvenient or needs improvement?</li>
            <li>• What are your biggest pain points with Zendesk?</li>
            <li>• What would save you the most time in your daily work?</li>
          </ul>
          <p className="mt-2 text-xs text-muted-foreground italic">
            Write freely in any language. Every comment helps!
          </p>
        </DialogHeader>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-6 pb-6 flex flex-col gap-4 min-h-0">
          {inbox.length > 0 && (
            <div className="rounded-md border border-emerald-300/60 bg-emerald-50/70 dark:bg-emerald-950/20 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <p className="text-sm font-medium text-emerald-800 dark:text-emerald-300">
                  New replies from admin ({inbox.length})
                </p>
                <Button size="sm" variant="outline" onClick={() => void markInboxRead()}>
                  Mark as read
                </Button>
              </div>
              <div className="mt-2 space-y-1">
                {inbox.slice(0, 5).map((m) => (
                  <p key={m.id} className="text-xs text-emerald-900/90 dark:text-emerald-200/90">
                    • {m.preview}
                  </p>
                ))}
              </div>
            </div>
          )}

          {/* Textarea */}
          <div className="flex flex-col gap-2">
            {editingEntry && (
              <div className="flex items-center justify-between text-xs text-amber-600 dark:text-amber-400 font-medium">
                <span>Editing entry from {formatDate(editingEntry.created_at)}</span>
                <button
                  onClick={cancelEdit}
                  className="flex items-center gap-1 hover:underline"
                >
                  <X className="size-3" /> Cancel edit
                </button>
              </div>
            )}
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Write your thoughts here..."
              rows={10}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm
                         ring-offset-background placeholder:text-muted-foreground
                         focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring
                         focus-visible:ring-offset-2 resize-y min-h-[120px] disabled:opacity-50"
              disabled={submitting}
            />
            {error && (
              <p className="text-xs text-destructive">{error}</p>
            )}
            <Button
              onClick={handleSubmit}
              disabled={submitting || !text.trim()}
              className="self-end"
            >
              {submitting
                ? (editingEntry ? 'Saving...' : 'Submitting...')
                : (editingEntry ? 'Save changes' : 'Submit feedback')
              }
            </Button>
          </div>

          {/* Previous entries */}
          {(entries.length > 0 || loadingEntries) && (
            <div className="flex flex-col gap-2">
              <div className="border-t pt-4">
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-3">
                  Your previous feedback
                </p>
                {loadingEntries ? (
                  <p className="text-xs text-muted-foreground">Loading...</p>
                ) : (
                  <div className="flex flex-col gap-2">
                    {entries.map((entry) => (
                      <div
                        key={entry.id}
                        className={`group rounded-md border p-3 text-sm transition-colors
                          ${editingEntry?.id === entry.id
                            ? 'border-amber-400 bg-amber-50 dark:bg-amber-950/20'
                            : 'border-border bg-muted/30 hover:bg-muted/50'
                          }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <p className="text-muted-foreground text-xs mb-1 shrink-0">
                            {formatDate(entry.updated_at !== entry.created_at
                              ? entry.updated_at
                              : entry.created_at)}
                            {entry.updated_at !== entry.created_at && (
                              <span className="ml-1 italic">(edited)</span>
                            )}
                          </p>
                          <button
                            onClick={() => startEdit(entry)}
                            className="shrink-0 text-muted-foreground hover:text-foreground
                                       opacity-0 group-hover:opacity-100 transition-opacity"
                            title="Edit this entry"
                          >
                            <Pencil className="size-3.5" />
                          </button>
                        </div>
                        <p className="whitespace-pre-wrap leading-relaxed">{entry.text}</p>
                        {(entry.replies ?? []).length > 0 && (
                          <div className="mt-2 pt-2 border-t space-y-1">
                            {(entry.replies ?? []).map((reply) => (
                              <div key={reply.id} className="rounded border bg-background px-2 py-1.5">
                                <p className="text-[11px] text-muted-foreground">
                                  {reply.author_name} • {formatDate(reply.created_at)}
                                </p>
                                <p className="whitespace-pre-wrap leading-relaxed mt-0.5">{reply.text}</p>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
