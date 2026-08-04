import { useEffect, useRef, useState, type FormEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import { BarChart3, Send, Loader2, ChevronDown, ChevronUp, Table2, BookOpen, ExternalLink, RotateCcw, RefreshCw, DatabaseZap } from 'lucide-react'
import { authHeaders } from '@/lib/api'
import { useAuth } from '@/contexts/AuthContext'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PageScroll } from '@/components/PageScroll'
import { REFERENCE_BRANDS } from '@/reference_data/fixtures'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  loadAnalyticsMessages,
  saveAnalyticsMessages,
  clearAnalyticsMessages,
  loadAnalyticsFilters,
  saveAnalyticsFilters,
} from '@/lib/chatStorage'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface TableData {
  columns: string[]
  rows: (string | number | boolean | null)[][]
}

interface KBResult {
  title: string
  url: string
  snippet: string
}

interface AnalyticsMessage {
  role: 'user' | 'assistant'
  content: string
  sql?: string | null
  rowCount?: number | null
  table?: TableData | null
  kbResults?: KBResult[] | null
}

// ---------------------------------------------------------------------------
// Suggested starter questions
// ---------------------------------------------------------------------------

const SUGGESTIONS = [
  'Which brands open the most tickets?',
  'What are the most common issue categories?',
  'What is the average resolution time by brand?',
  'How many tickets came in by channel (web vs email)?',
  'Which stores have the most recurring issues?',
  'Show me escalated tickets by brand',
  'What are the top root causes this month?',
  'Which engineers handle the most tickets?',
  'How do I reset a POS terminal?',
  'Are there KB articles about network connectivity issues?',
]

const TIMEFRAME_OPTIONS = [
  { label: 'Last 7 days',    value: '7' },
  { label: 'Last 30 days',   value: '30' },
  { label: 'Last 90 days',   value: '90' },
  { label: 'Last 6 months',  value: '180' },
  { label: 'Last 12 months', value: '365' },
]

const BRAND_OPTIONS = [
  { label: 'All brands',          value: 'all' },
  ...REFERENCE_BRANDS.map(brand => ({ label: brand, value: brand })),
]

// ---------------------------------------------------------------------------
// Result table — actual BQ data, independent of AI text
// ---------------------------------------------------------------------------

function ResultTable({ table, rowCount }: { table: TableData; rowCount: number | null }) {
  const [open, setOpen] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const PREVIEW_ROWS = 10
  const displayRows = showAll ? table.rows : table.rows.slice(0, PREVIEW_ROWS)
  const hasMore = table.rows.length > PREVIEW_ROWS

  return (
    <div className="mt-2 rounded border border-border bg-muted/40 text-xs">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-muted-foreground hover:text-foreground transition-colors"
      >
        <Table2 className="size-3.5 shrink-0" />
        <span>Data table</span>
        {rowCount != null && (
          <span className="ml-1 text-[10px] rounded bg-muted px-1.5 py-0.5 font-mono">
            {rowCount} rows
          </span>
        )}
        {open ? <ChevronUp className="ml-auto size-3.5" /> : <ChevronDown className="ml-auto size-3.5" />}
      </button>
      {open && (
        <div className="px-3 pb-3 overflow-x-auto">
          <table className="w-full border-collapse text-[11px] font-mono">
            <thead>
              <tr>
                {table.columns.map(col => (
                  <th
                    key={col}
                    className="border border-border bg-muted px-2 py-1 text-left font-semibold text-muted-foreground whitespace-nowrap"
                  >
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {displayRows.map((row, i) => (
                <tr key={i} className={i % 2 === 0 ? 'bg-background' : 'bg-muted/20'}>
                  {row.map((cell, j) => (
                    <td
                      key={j}
                      className="border border-border px-2 py-1 whitespace-nowrap text-foreground"
                    >
                      {cell === null ? <span className="text-muted-foreground italic">NULL</span> : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {hasMore && !showAll && (
            <button
              onClick={() => setShowAll(true)}
              className="mt-1.5 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
            >
              Show all {table.rows.length} rows…
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// KB articles block — collapsible list of relevant articles
// ---------------------------------------------------------------------------

function KBArticles({ results }: { results: KBResult[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-1 rounded border border-border bg-muted/40 text-xs">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-muted-foreground hover:text-foreground transition-colors"
      >
        <BookOpen className="size-3.5 shrink-0" />
        <span>KB Articles</span>
        <span className="ml-1 text-[10px] rounded bg-muted px-1.5 py-0.5 font-mono">
          {results.length}
        </span>
        {open ? <ChevronUp className="ml-auto size-3.5" /> : <ChevronDown className="ml-auto size-3.5" />}
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          {results.map((r, i) => (
            <div key={i} className="rounded border border-border bg-background p-2">
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-start gap-1 font-medium text-primary hover:underline"
              >
                {r.title}
                <ExternalLink className="size-3 mt-0.5 shrink-0" />
              </a>
              {r.snippet && (
                <p className="mt-0.5 text-[11px] text-muted-foreground leading-relaxed line-clamp-3">
                  {r.snippet.replace(/<[^>]+>/g, '')}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Message bubble
// ---------------------------------------------------------------------------

function MessageBubble({ msg, onRerun }: { msg: AnalyticsMessage; onRerun?: () => void }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
          {msg.content}
        </div>
      </div>
    )
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-[90%] w-full">
        <div className="flex items-center gap-2 mb-1">
          <BarChart3 className="size-4 text-muted-foreground" />
          <span className="text-xs font-medium text-muted-foreground">Analytics</span>
        </div>
        <div className="rounded-2xl rounded-tl-sm border bg-card px-4 py-3 text-sm leading-relaxed prose prose-sm dark:prose-invert max-w-none">
          <ReactMarkdown>{msg.content}</ReactMarkdown>
        </div>
        {/* Table first — source of truth for ticket data */}
        {msg.table && <ResultTable table={msg.table} rowCount={msg.rowCount ?? null} />}
        {/* KB articles — source of truth for knowledge base */}
        {msg.kbResults && msg.kbResults.length > 0 && <KBArticles results={msg.kbResults} />}
        {/* Re-run button — only on the last assistant message */}
        {onRerun && (
          <button
            onClick={onRerun}
            className="mt-2 flex items-center gap-1.5 text-xs font-medium text-primary hover:text-primary/80 transition-colors border border-primary/30 hover:border-primary/60 rounded-md px-2.5 py-1"
          >
            <RefreshCw className="size-3.5" />
            Re-run with current filters
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function AnalyticsPage() {
  const { isAdmin } = useAuth()

  // Initialise state from localStorage on first render
  const [messages, setMessages] = useState<AnalyticsMessage[]>(() => loadAnalyticsMessages())
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [timeframe, setTimeframe] = useState<string>(() => loadAnalyticsFilters().timeframe)
  const [brand, setBrand] = useState<string>(() => loadAnalyticsFilters().brand)
  const [syncing, setSyncing] = useState(false)
  const [syncProgress, setSyncProgress] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  // Persist messages to localStorage whenever they change
  useEffect(() => {
    saveAnalyticsMessages(messages)
  }, [messages])

  // Persist filters to localStorage whenever they change
  useEffect(() => {
    saveAnalyticsFilters(timeframe, brand)
  }, [timeframe, brand])

  // "New conversation" — clear localStorage and reset state
  const handleNewConversation = () => {
    clearAnalyticsMessages()
    setMessages([])
  }

  // Admin: trigger manual BQ sync, then poll progress every 3s
  async function handleSync() {
    if (syncing) return
    setSyncing(true)
    setSyncProgress('Starting...')
    try {
      const res = await fetch('/api/admin/sync?days=2', {
        method: 'POST',
        headers: await authHeaders(),
      })
      const data = await res.json() as { status: string }
      if (data.status !== 'started' && data.status !== 'already_running') {
        setSyncProgress('Failed to start sync')
        setTimeout(() => setSyncProgress(null), 6000)
        setSyncing(false)
        return
      }
    } catch {
      setSyncProgress('Failed to start sync')
      setTimeout(() => setSyncProgress(null), 6000)
      setSyncing(false)
      return
    }

    // Poll /api/admin/sync/status every 3s until done
    const poll = async () => {
      try {
        const res = await fetch('/api/admin/sync/status', {
          headers: await authHeaders(),
        })
        const data = await res.json() as {
          running: boolean
          progress: string | null
          last_result?: { tickets: number; kb_articles: number } | null
          last_error?: string | null
        }
        setSyncProgress(data.progress ?? (data.running ? 'Running...' : 'Done'))
        if (data.running) {
          setTimeout(() => void poll(), 3000)
        } else {
          setSyncing(false)
          setTimeout(() => setSyncProgress(null), 10000)
        }
      } catch {
        setSyncing(false)
        setSyncProgress(null)
      }
    }
    setTimeout(() => void poll(), 2000)
  }

  const scrollToBottom = () =>
    setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), 50)

  async function sendMessage(question: string) {
    if (!question.trim() || loading) return

    const userMsg: AnalyticsMessage = { role: 'user', content: question }
    const updatedMessages = [...messages, userMsg]
    setMessages(updatedMessages)
    setInput('')
    setLoading(true)
    scrollToBottom()

    const assistantIdx = updatedMessages.length
    setMessages(prev => [...prev, { role: 'assistant', content: '' }])

    try {
      const res = await fetch('/api/analytics/chat/stream', {
        method: 'POST',
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          messages: updatedMessages.map(m => ({ role: m.role, content: m.content })),
          timeframe_days: parseInt(timeframe),
          brand_filter: brand || null,
        }),
      })

      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let sql: string | null = null
      let rowCount: number | null = null
      let table: TableData | null = null
      let kbResults: KBResult[] | null = null

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const evt = JSON.parse(line.slice(6))
            if (evt.delta) {
              setMessages(prev => {
                const next = [...prev]
                next[assistantIdx] = {
                  ...next[assistantIdx],
                  content: next[assistantIdx].content + evt.delta,
                }
                return next
              })
              scrollToBottom()
            } else if (evt.done) {
              sql = evt.sql ?? null
              rowCount = evt.row_count ?? null
              table = evt.table ?? null
              kbResults = evt.kb_results ?? null
            } else if (evt.error) {
              setMessages(prev => {
                const next = [...prev]
                next[assistantIdx] = {
                  ...next[assistantIdx],
                  content: `Error: ${evt.error}`,
                }
                return next
              })
            }
          } catch {
            // skip malformed lines
          }
        }
      }

      // Attach metadata to the assistant message
      if (sql || table || kbResults) {
        setMessages(prev => {
          const next = [...prev]
          next[assistantIdx] = { ...next[assistantIdx], sql, rowCount, table, kbResults }
          return next
        })
      }
    } catch (err) {
      setMessages(prev => {
        const next = [...prev]
        next[assistantIdx] = {
          ...next[assistantIdx],
          content: `Failed to get response: ${err instanceof Error ? err.message : String(err)}`,
        }
        return next
      })
    } finally {
      setLoading(false)
      scrollToBottom()
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    void sendMessage(input)
  }

  return (
    <PageScroll>
    <div className="p-6 max-w-3xl mr-auto space-y-4">
      {/* Header + filters */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <BarChart3 className="size-5 text-primary" />
            <h1 className="text-lg font-semibold">Ticket Analytics</h1>
          </div>
          <p className="text-[13px] text-sky-600 dark:text-sky-400 pl-7">Ask about trends, patterns, and stats across all tickets: "How many Orbit View issues last month?", "Top categories this week?"</p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={timeframe} onValueChange={setTimeframe}>
            <SelectTrigger className="h-8 w-36 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TIMEFRAME_OPTIONS.map(o => (
                <SelectItem key={o.value} value={o.value} className="text-xs">{o.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={brand === '' ? 'all' : brand} onValueChange={v => setBrand(v === 'all' ? '' : v)}>
            <SelectTrigger className="h-8 w-40 text-xs">
              <SelectValue placeholder="All brands" />
            </SelectTrigger>
            <SelectContent>
              {BRAND_OPTIONS.map(o => (
                <SelectItem key={o.value} value={o.value} className="text-xs">{o.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {isAdmin && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => void handleSync()}
              disabled={syncing}
              title="Sync latest data from Zendesk now"
            >
              {syncing
                ? <Loader2 className="size-3.5 animate-spin" />
                : <DatabaseZap className="size-3.5" />}
              Sync now
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
            onClick={handleNewConversation}
            disabled={messages.length === 0}
            title="Start a new conversation"
          >
            <RotateCcw className="size-3.5" />
            New
          </Button>
        </div>
      </div>
      {syncProgress && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground bg-muted rounded px-3 py-1.5">
          {syncing && <Loader2 className="size-3 animate-spin shrink-0" />}
          <span>{syncProgress}</span>
        </div>
      )}

      {/* Empty state */}
      {messages.length === 0 && (
        <div className="pt-4">
          <p className="text-center text-[13px] text-sky-600 dark:text-sky-400 mb-4">
            Ask about trends, patterns, and stats across all tickets: "How many Orbit View issues last month?", "Top categories this week?"
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {SUGGESTIONS.map(s => (
              <button
                key={s}
                onClick={() => void sendMessage(s)}
                className="text-left rounded-lg border bg-card p-3 text-sm hover:bg-accent transition-colors"
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Messages */}
      {messages.map((msg, i) => {
        // Find the last assistant message index to attach re-run button
        const lastAssistantIdx = messages.reduceRight(
          (found, m, idx) => (found === -1 && m.role === 'assistant' ? idx : found),
          -1,
        )
        const isLastAssistant = msg.role === 'assistant' && i === lastAssistantIdx
        const lastUserQuestion = isLastAssistant
          ? [...messages].slice(0, i).reverse().find(m => m.role === 'user')?.content
          : undefined
        return (
          <MessageBubble
            key={i}
            msg={msg}
            onRerun={
              isLastAssistant && lastUserQuestion && !loading
                ? () => void sendMessage(lastUserQuestion)
                : undefined
            }
          />
        )
      })}

      {/* Loading */}
      {loading && messages[messages.length - 1]?.content === '' && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="size-4 animate-spin" />
          <span>Analyzing tickets...</span>
        </div>
      )}

      <div ref={bottomRef} />

      {/* Input */}
      <form onSubmit={handleSubmit} className="flex gap-2 pt-2">
        <Input
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Ask about trends, patterns, and stats across all tickets…"
          disabled={loading}
          className="flex-1"
        />
        <Button type="submit" disabled={loading || !input.trim()} size="icon">
          {loading ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
        </Button>
      </form>
    </div>
    </PageScroll>
  )
}
