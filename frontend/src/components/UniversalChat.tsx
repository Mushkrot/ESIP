/**
 * Universal Chat panel.
 *
 * Connects to POST /api/universal/stream.
 * Props:
 *   ticketId       — when set, injects this ticket as context into the backend
 *   onTicketFound  — called when the engineer clicks "Open in panel" on a ticket card
 *   expanded       — controls whether the expand toggle is shown as "collapse"
 *   onExpandToggle — called when the expand/collapse button is clicked
 */
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { Bot, Send, Loader2, RotateCcw, ExternalLink, Maximize2, Minimize2, Languages, Check, ChevronDown, ChevronRight, Sparkles, Square, Copy, PencilLine } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { authHeaders } from '@/lib/api'
import { trackImpactEvent } from '@/lib/impactTelemetry'
import { slugToTitle } from '@/lib/utils'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const CHAT_LANGUAGES = [
  { code: 'en', label: 'EN', name: 'English' },
  { code: 'fr', label: 'FR', name: 'French' },
  { code: 'es', label: 'ES', name: 'Spanish' },
  { code: 'zh', label: 'ZH', name: 'Chinese' },
  { code: 'ru', label: 'RU', name: 'Russian' },
  { code: 'hi', label: 'HI', name: 'Hindi' },
] as const
type ChatLangCode = typeof CHAT_LANGUAGES[number]['code']

const LANG_NAMES: Record<ChatLangCode, string> = {
  en: 'English', fr: 'French', es: 'Spanish', zh: 'Chinese', ru: 'Russian', hi: 'Hindi',
}

const LIVE_STATUS_STEPS = [
  'Checking ticket context...',
  'Searching indexed procedures...',
  'Checking analytics...',
  'The first result looks incomplete, checking another source...',
  'Preparing the best available answer...',
] as const

// Marker prefix so we can detect translate-requests in the message list
const TRANSLATE_PREFIX = '__translate__:'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface TicketCard {
  id: number
  subject: string
  org_name: string
  brand: string
  status: string
  store_id: string
  issue_category: string
  resolution: string
  solved_at: string | null
}

interface Source {
  type?: string
  title?: string
  snippet?: string
  url?: string
}

interface AnswerReview {
  quality: 'good' | 'weak' | 'likely_wrong' | 'insufficient_evidence'
  confidence: number
  problem_types: string[]
  recommended_action: string
  reformulated_query?: string | null
  explanation: string
  user_visible_note: string
  improved_answer?: string | null
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  tickets?: TicketCard[]
  sources?: Source[]
  traceId?: string
  turnId?: string
  review?: AnswerReview
}

interface UniversalChatProps {
  ticketId?: number | null
  onTicketFound?: (ticketId: number) => void
  expanded?: boolean
  onExpandToggle?: () => void
  storageKey?: string
  /** When set, fills the chat input once (parent should clear via onDraftMessageApplied). Does not send. */
  draftMessage?: string | null
  onDraftMessageApplied?: () => void
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function loadHistory(key: string): Message[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const { messages, ts } = JSON.parse(raw)
    if (Date.now() - ts > 7 * 24 * 60 * 60 * 1000) return []
    return messages
  } catch {
    return []
  }
}

function saveHistory(key: string, messages: Message[]) {
  try {
    localStorage.setItem(key, JSON.stringify({ messages: messages.slice(-30), ts: Date.now() }))
  } catch {
    // Private browsing and storage policies may make history unavailable.
  }
}

function makeConversationId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return `uc_${crypto.randomUUID()}`
  }
  return `uc_${Date.now()}_${Math.random().toString(16).slice(2)}`
}

function loadConversationId(key: string): string {
  const storageKey = `${key}:conversation_id`
  try {
    const existing = localStorage.getItem(storageKey)
    if (existing) return existing
    const created = makeConversationId()
    localStorage.setItem(storageKey, created)
    return created
  } catch {
    return makeConversationId()
  }
}

// Convert ticket references to markdown links. Bare 4-digit #NNNN values are
// usually store numbers in ESIP, so only explicit "ticket #NNNN" gets linked.
const _TICKET_EXPLICIT_RE = /\b(ticket|тикет)\s+#?(\d{4,7})\b/gi
const _TICKET_BARE_RE = /(?<!\[)#(\d{5,7})(?!\d)(?![^[]*\])/g
const ZENDESK_TICKET_URL_RE = /^https:\/\/example\.zendesk\.com\/agent\/tickets\/(\d{4,7})(?:[/?#].*)?$/i

function linkifyTickets(text: string): string {
  return text
    .replace(_TICKET_EXPLICIT_RE, '$1 [#$2](https://support.example.com/agent/tickets/$2)')
    .replace(_TICKET_BARE_RE, '[#$1](https://support.example.com/agent/tickets/$1)')
}

function parseZendeskTicketId(href?: string): number | null {
  if (!href) return null
  const match = href.match(ZENDESK_TICKET_URL_RE)
  if (!match) return null
  const ticketId = parseInt(match[1], 10)
  return Number.isFinite(ticketId) ? ticketId : null
}

function normalizeVisibleAnswerMarkdown(text: string): string {
  const bulletCountRe = /^(\s*(?:[-*]|•)\s+)([\s\S]*?)(\s+(?:\(\d+\s+tickets?\)|with\s+\d+\s+tickets?\.?))(\s*)$/i
  let inCodeFence = false
  let inMachineFooter = false
  return text.split('\n').map(line => {
    const trimmed = line.trim()
    if (trimmed.startsWith('```')) {
      inCodeFence = !inCodeFence
      return line
    }
    if (trimmed.startsWith('TICKETS_JSON:') || trimmed.startsWith('SOURCES_JSON:') || trimmed.startsWith('TRACE_JSON:')) {
      inMachineFooter = true
      return line
    }
    if (inCodeFence || inMachineFooter) return line
    return line.replace(bulletCountRe, (match, prefix, label, count, suffix) => {
      const cleanLabel = String(label).replace(/\*\*/g, '').trim()
      if (cleanLabel.includes('[') || cleanLabel.includes('](') || cleanLabel.length > 160) return match
      if (!cleanLabel) return match
      return `${prefix}**${cleanLabel}**${count}${suffix}`
    })
  }).join('\n')
}

function cleanContent(text: string, options: { linkify?: boolean } = {}): string {
  const stripped = text
    .replace(/TICKETS_JSON:.*$/ms, '')
    .replace(/SOURCES_JSON:.*$/ms, '')
    .replace(/TRACE_JSON:.*$/ms, '')
    .replace(/^\s*(?:DEBUG|TRACE(?:_ID)?)\s*:.*$/gim, '')
    .replace(/^\s*(?:Tool Call|Tool Output|Data Retrieved):\s*\n(?:```[\s\S]*?```\s*)?(?=^\s*(?:Tool Call|Tool Output|Data Retrieved):|\S|\s*$)/gim, '')
    .replace(/^\s*(?:Tool Call|Tool Output|Data Retrieved):\s*\n(?:```[\s\S]*?```\s*|\{[\s\S]*?\}\s*)(?=^\s*(?:Tool Call|Tool Output|Data Retrieved):|^[A-ZА-Я0-9][^\n]{0,160}|\s*$)/gim, '')
    .replace(/^\s*(?:Tool Call|Tool Output|Data Retrieved):\s*\n[\s\S]*?(?=^\s*(?:Tool Call|Tool Output|Data Retrieved):|^[A-ZА-Я0-9][^\n]{0,160}|\s*$)/gim, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
  const normalized = normalizeVisibleAnswerMarkdown(stripped)
  return options.linkify === false ? normalized : linkifyTickets(normalized)
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

async function copyCleanTextToClipboard(text: string): Promise<void> {
  const plain = text.trim()
  if (!plain) return
  const html = `<div>${escapeHtml(plain).replace(/\n/g, '<br>')}</div>`
  const clipboard = navigator.clipboard
  if (clipboard && 'write' in clipboard && typeof ClipboardItem !== 'undefined') {
    try {
      await clipboard.write([
        new ClipboardItem({
          'text/plain': new Blob([plain], { type: 'text/plain' }),
          'text/html': new Blob([html], { type: 'text/html' }),
        }),
      ])
      return
    } catch {
      // Fall back to plain-text copy below.
    }
  }
  if (!clipboard) return
  await clipboard.writeText(plain)
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function isInsideMarkdownLinkText(text: string, offset: number): boolean {
  const lineStart = text.lastIndexOf('\n', offset) + 1
  const before = text.slice(lineStart, offset)
  const open = before.lastIndexOf('[')
  const close = before.lastIndexOf(']')
  if (open <= close) return false
  const closing = text.indexOf('](', offset)
  const nextNewline = text.indexOf('\n', offset)
  return closing !== -1 && (nextNewline === -1 || closing < nextNewline)
}

function linkifySources(text: string, sources?: Source[]): string {
  if (!text || !sources?.length) return text

  const candidates = sources
    .map(normalizeSourceForDisplay)
    .filter(source => source.url && source.title && source.title.length >= 8)
    .sort((a, b) => b.title.length - a.title.length)

  let out = text
  const seen = new Set<string>()
  for (const source of candidates) {
    const title = source.title.trim()
    const url = source.url.trim()
    const key = `${title.toLowerCase()}|${url}`
    if (seen.has(key)) continue
    seen.add(key)

    const pattern = new RegExp(escapeRegExp(title), 'g')
    out = out.replace(pattern, (match, offset: number, full: string) => {
      if (isInsideMarkdownLinkText(full, offset)) return match
      return `[${match}](${url})`
    })
  }
  return out
}

function statusVariant(s: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (s === 'open') return 'destructive'
  if (s === 'pending') return 'default'
  if (s === 'solved' || s === 'closed') return 'secondary'
  return 'outline'
}

const DRIVE_FILE_ID_RE = /^[A-Za-z0-9_-]{25,80}$/

function normalizeSourceForDisplay(source: Source) {
  const title = (source.title || '').trim()
  const url = (source.url || '').trim()
  const rawId = DRIVE_FILE_ID_RE.test(url)
    ? url
    : DRIVE_FILE_ID_RE.test(title)
      ? title
      : null
  if (!rawId) return { title, url }
  return {
    title: title === rawId ? 'Google Drive file' : title,
    url: `https://drive.google.com/file/d/${rawId}/view`,
  }
}

const SUGGESTIONS_NO_TICKET = [
  'Summarize the Voice Gateway configuration guide',
  'How many network modem tickets were opened last month?',
  'How was the POS printer paper jam issue at Cedar Grill resolved before?',
  'What is the procedure for resetting a Voice Gateway?',
  'Find similar cases where internet went down at Metro Burger',
  'Which brands had the most network outage tickets this quarter?',
]

const SUGGESTIONS_WITH_TICKET = [
  'Find similar past tickets',
  'What does the KB say about this issue?',
  'How was this resolved at other stores?',
  'Are there any relevant procedures or guides?',
  'How many times has this category of issue been reported?',
]

// ---------------------------------------------------------------------------
// Ticket card — ESIP panel first, Zendesk as secondary option
// ---------------------------------------------------------------------------

function TicketResultCard({
  ticket,
  onOpenInPanel,
}: {
  ticket: TicketCard
  onOpenInPanel?: (id: number) => void
}) {
  return (
    <div className="rounded-lg border bg-card p-3 space-y-1">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs text-muted-foreground">#{ticket.id}</span>
          <Badge variant={statusVariant(ticket.status)} className="text-xs">{ticket.status}</Badge>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {onOpenInPanel && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs text-primary"
              onClick={() => {
                void trackImpactEvent({
                  event_type: 'similar_ticket_opened',
                  workflow: 'Similar cases',
                  ticket_id: ticket.id,
                  object_type: 'ticket',
                  object_id: String(ticket.id),
                  object_label: ticket.subject,
                })
                onOpenInPanel(ticket.id)
              }}
            >
              Open left
            </Button>
          )}
          <a
            href={`https://support.example.com/agent/tickets/${ticket.id}`}
            target="_blank"
            rel="noreferrer"
            onClick={() => void trackImpactEvent({
              event_type: 'ticket_link_clicked',
              workflow: 'Source clicks',
              ticket_id: ticket.id,
              object_type: 'ticket',
              object_id: String(ticket.id),
              object_label: ticket.subject,
              target_url: `https://support.example.com/agent/tickets/${ticket.id}`,
            })}
          >
            <Button variant="ghost" size="sm" className="h-6 px-2 text-xs text-muted-foreground">
              <ExternalLink className="size-3" />
            </Button>
          </a>
        </div>
      </div>
      <p className="text-sm font-medium leading-snug line-clamp-2">{ticket.subject}</p>
      {ticket.org_name && (
        <p className="text-xs text-muted-foreground">{ticket.org_name}</p>
      )}
      {ticket.issue_category && (
        <p className="text-xs text-muted-foreground">{slugToTitle(ticket.issue_category)}</p>
      )}
      {ticket.resolution && (
        <p className="text-xs text-muted-foreground border-t pt-1 mt-1 line-clamp-2">
          <span className="font-medium">Resolution:</span> {ticket.resolution}
        </p>
      )}
    </div>
  )
}

function TicketResultsGroup({
  tickets,
  onOpenInPanel,
}: {
  tickets: TicketCard[]
  onOpenInPanel?: (id: number) => void
}) {
  const ticketSignature = useMemo(() => tickets.map(t => t.id).join(','), [tickets])
  const [expandedSignature, setExpandedSignature] = useState<string | null>(null)
  const expanded = expandedSignature === ticketSignature
  const preview = tickets.slice(0, 5).map(t => `#${t.id}`).join(', ')

  return (
    <div className="w-full max-w-[95%] rounded-lg border bg-card">
      <button
        type="button"
        onClick={() => {
          const next = !expanded
          setExpandedSignature(next ? ticketSignature : null)
          if (next) {
            void trackImpactEvent({
              event_type: 'similar_tickets_viewed',
              workflow: 'Similar cases',
              metadata: { ticket_ids: tickets.map(t => t.id), count: tickets.length },
            })
          }
        }}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/50"
      >
        <div className="min-w-0">
          <div className="text-xs font-medium">
            Related tickets ({tickets.length})
          </div>
          <div className="text-xs text-muted-foreground truncate">
            {preview}{tickets.length > 5 ? ', ...' : ''}
          </div>
        </div>
        <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
          {expanded ? 'Hide' : 'Show'}
          {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </div>
      </button>
      {expanded && (
        <div className="space-y-2 border-t p-2">
          {tickets.map(t => (
            <TicketResultCard
              key={t.id}
              ticket={t}
              onOpenInPanel={onOpenInPanel}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Message bubble
// ---------------------------------------------------------------------------

function TicketInlineLink({
  ticketId,
  href,
  children,
  onOpenInPanel,
}: {
  ticketId: number
  href: string
  children: ReactNode
  onOpenInPanel?: (id: number) => void
}) {
  if (!onOpenInPanel) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        className="text-primary underline underline-offset-2 hover:opacity-75"
      >
        {children}
      </a>
    )
  }

  return (
    <span className="mx-0.5 inline-flex items-center overflow-hidden rounded-md border border-primary/25 bg-background align-baseline text-xs leading-none shadow-xs">
      <button
        type="button"
        onClick={() => {
          void trackImpactEvent({
            event_type: 'ticket_link_clicked',
            workflow: 'Source clicks',
            ticket_id: ticketId,
            object_type: 'ticket',
            object_id: String(ticketId),
            target_url: href,
          })
          onOpenInPanel(ticketId)
        }}
        className="inline-flex h-6 items-center px-1.5 font-mono font-medium text-primary hover:bg-primary/10"
        title={`Open ticket #${ticketId} in the left panel`}
      >
        {children}
      </button>
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        onClick={() => void trackImpactEvent({
          event_type: 'ticket_link_clicked',
          workflow: 'Source clicks',
          ticket_id: ticketId,
          object_type: 'ticket',
          object_id: String(ticketId),
          target_url: href,
        })}
        className="inline-flex h-6 w-6 items-center justify-center border-l border-primary/20 text-muted-foreground hover:bg-muted hover:text-foreground"
        title={`Open ticket #${ticketId} in Zendesk`}
      >
        <ExternalLink className="size-3" />
      </a>
    </span>
  )
}

function makeMarkdownComponents(onOpenInPanel?: (id: number) => void): Components {
  return {
    a: ({ href, children }) => {
      const ticketId = parseZendeskTicketId(href)
      if (ticketId && href) {
        return (
          <TicketInlineLink
            ticketId={ticketId}
            href={href}
            onOpenInPanel={onOpenInPanel}
          >
            {children}
          </TicketInlineLink>
        )
      }

      return (
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          className="text-primary underline underline-offset-2 hover:opacity-75"
        >
          {children}
        </a>
      )
    },
    table: ({ children }) => (
      <div className="my-2 max-w-full overflow-x-auto rounded-md border bg-background/70">
        <table className="w-full min-w-[520px] border-collapse text-xs">
          {children}
        </table>
      </div>
    ),
    th: ({ children }) => (
      <th className="border-b bg-muted/70 px-2 py-1.5 text-left align-top text-[11px] font-semibold uppercase tracking-normal text-muted-foreground">
        {children}
      </th>
    ),
    td: ({ children }) => (
      <td className="border-b px-2 py-1.5 align-top leading-snug last:border-b-0">
        {children}
      </td>
    ),
  }
}

function MessageBubble({
  msg,
  index,
  onOpenInPanel,
  onImproveAnswer,
  onEditMessage,
  reviewing,
}: {
  msg: Message
  index: number
  onOpenInPanel?: (id: number) => void
  onImproveAnswer?: () => void
  onEditMessage?: (index: number, text: string) => void
  reviewing?: boolean
}) {
  const isUser = msg.role === 'user'
  const isTranslate = isUser && msg.content.startsWith(TRANSLATE_PREFIX)
  const cleanBase = isTranslate ? '' : cleanContent(msg.content, { linkify: !isUser })
  const clean = !isUser ? linkifySources(cleanBase, msg.sources) : cleanBase
  const copyText = isUser ? cleanBase : cleanContent(msg.content, { linkify: false })
  const [copied, setCopied] = useState(false)
  const markdownComponents = useMemo(
    () => makeMarkdownComponents(onOpenInPanel),
    [onOpenInPanel],
  )

  const handleCopy = async () => {
    try {
      await copyCleanTextToClipboard(copyText)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch {
      setCopied(false)
    }
  }

  // Translate-request: show as a small pill, not a full bubble
  if (isTranslate) {
    const lang = msg.content.slice(TRANSLATE_PREFIX.length) as ChatLangCode
    return (
      <div className="flex justify-end">
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground bg-muted/60 rounded-full px-2.5 py-0.5 border">
          <Languages className="size-3" />
          Translated to {LANG_NAMES[lang]}
        </span>
      </div>
    )
  }

  return (
    <div className={`flex flex-col gap-2 ${isUser ? 'items-end' : 'items-start'}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-relaxed ${
          isUser
            ? 'bg-primary text-primary-foreground'
            : 'bg-muted text-foreground'
        }`}
      >
        {isUser ? (
          <span className="whitespace-pre-wrap">{clean || <span className="opacity-50 italic">…</span>}</span>
        ) : clean ? (
          <div className="prose prose-sm dark:prose-invert max-w-none
            [&_p]:my-1 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0
            [&_ul]:my-1 [&_li]:my-0.5
            [&_ol]:my-1
            [&_strong]:font-semibold
            [&_h1]:text-sm [&_h2]:text-sm [&_h3]:text-xs [&_h3]:font-semibold">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={markdownComponents}
            >
              {clean}
            </ReactMarkdown>
          </div>
        ) : (
          <span className="opacity-50 italic">…</span>
        )}
      </div>

      {copyText && (
        <div className={`flex w-full max-w-[85%] items-center gap-2 text-[11px] text-muted-foreground ${isUser ? 'justify-end' : 'justify-start'}`}>
          <button
            type="button"
            onClick={() => void handleCopy()}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground"
            title="Copy this message"
          >
            {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
          {isUser && (
            <button
              type="button"
              onClick={() => onEditMessage?.(index, msg.content)}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground"
              title="Edit this question and resend"
            >
              <PencilLine className="size-3" />
              Edit
            </button>
          )}
        </div>
      )}

      {msg.tickets && msg.tickets.length > 0 && (
        <TicketResultsGroup tickets={msg.tickets} onOpenInPanel={onOpenInPanel} />
      )}

      {msg.sources && msg.sources.filter(s => s.url).length > 0 && (
        <div className="w-full max-w-[90%] space-y-1">
          {msg.sources.filter(s => s.url).map((s, i) => {
            const source = normalizeSourceForDisplay(s)
            return (
            <a
              key={i}
              href={source.url}
              target="_blank"
              rel="noreferrer"
              onClick={() => void trackImpactEvent({
                event_type: 'source_link_clicked',
                workflow: 'Source clicks',
                object_type: 'source',
                object_id: source.url,
                object_label: source.title,
                target_url: source.url,
              })}
              className="flex items-center gap-1.5 text-xs text-primary hover:underline"
            >
              <ExternalLink className="size-3 shrink-0" />
              {source.title || source.url}
            </a>
            )
          })}
        </div>
      )}

      {!isUser && msg.traceId && clean && (
        <div className="flex w-full max-w-[90%] items-center justify-between gap-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs shadow-sm dark:border-amber-900/60 dark:bg-amber-950/30">
          <div className="min-w-0">
            <div className="font-medium text-amber-950 dark:text-amber-100">Answer looks weak?</div>
            <div className="text-amber-800 dark:text-amber-200">Re-check sources and rewrite it.</div>
          </div>
          <Button
            type="button"
            size="sm"
            className="h-8 shrink-0 gap-1.5 bg-amber-500 px-3 text-xs font-semibold text-white shadow-sm hover:bg-amber-600 disabled:opacity-70"
            onClick={onImproveAnswer}
            disabled={reviewing}
            title="Ask the system to rewrite this answer after checking its sources and routing trace"
          >
            {reviewing ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
            {reviewing ? 'Checking...' : 'Improve answer'}
          </Button>
        </div>
      )}

      {!isUser && msg.review && (
        <div className="w-full max-w-[90%] rounded-md border bg-card p-3 text-xs space-y-2">
          {msg.review.improved_answer ? (
            <>
              <div className="mb-1 font-medium">Improved answer</div>
              <div className="prose prose-sm dark:prose-invert max-w-none
                [&_p]:my-1 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0
                [&_ul]:my-1 [&_li]:my-0.5 [&_ol]:my-1">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                  {linkifyTickets(msg.review.improved_answer)}
                </ReactMarkdown>
              </div>
              <div className="flex flex-wrap items-center gap-2 border-t pt-2 text-[11px] text-muted-foreground">
                <Badge variant={msg.review.quality === 'good' ? 'secondary' : 'outline'} className="text-[11px]">
                  {msg.review.quality.replace('_', ' ')}
                </Badge>
                <span>Confidence {Math.round((msg.review.confidence || 0) * 100)}%</span>
                {msg.review.user_visible_note && <span>{msg.review.user_visible_note}</span>}
              </div>
            </>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={msg.review.quality === 'good' ? 'secondary' : 'outline'} className="text-[11px]">
                  Review: {msg.review.quality.replace('_', ' ')}
                </Badge>
                <span className="text-muted-foreground">
                  Confidence {Math.round((msg.review.confidence || 0) * 100)}%
                </span>
              </div>
              <p className="text-muted-foreground">{msg.review.user_visible_note}</p>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function UniversalChat({
  ticketId,
  onTicketFound,
  expanded = false,
  onExpandToggle,
  storageKey = 'universal_chat_history',
  draftMessage = null,
  onDraftMessageApplied,
}: UniversalChatProps) {
  const [messages, setMessages] = useState<Message[]>(() => loadHistory(storageKey))
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [reviewingIndex, setReviewingIndex] = useState<number | null>(null)
  const [editingMessageIndex, setEditingMessageIndex] = useState<number | null>(null)
  const [liveStatus, setLiveStatus] = useState('')
  const [currentTurnId, setCurrentTurnId] = useState<string | null>(null)
  const [answerNowPending, setAnswerNowPending] = useState(false)
  const [answerNowRequestedTurnId, setAnswerNowRequestedTurnId] = useState<string | null>(null)
  const [answerNowError, setAnswerNowError] = useState('')
  const [chatLang, setChatLang] = useState<ChatLangCode>('en')
  const messageRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const answerNowInFlightRef = useRef<string | null>(null)
  const statusActiveRef = useRef(false)
  const statusTimersRef = useRef<number[]>([])
  const pendingAssistantScrollIdx = useRef<number | null>(null)
  const conversationIdRef = useRef<string>(loadConversationId(storageKey))

  // Reset chat when ticket changes (new ticket context = new conversation)
  useEffect(() => {
    setMessages(loadHistory(storageKey))
    conversationIdRef.current = loadConversationId(storageKey)
  }, [ticketId, storageKey])

  useEffect(() => {
    if (draftMessage == null || draftMessage === '') return
    setInput(draftMessage)
    onDraftMessageApplied?.()
  }, [draftMessage, onDraftMessageApplied])

  useEffect(() => {
    saveHistory(storageKey, messages)
  }, [messages, storageKey])

  useEffect(() => () => {
    abortRef.current?.abort()
    statusTimersRef.current.forEach(timer => window.clearTimeout(timer))
    statusTimersRef.current = []
  }, [])

  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    if (!input.trim()) {
      el.style.height = ''
      return
    }
    const maxAutoHeight = 144
    const currentHeight = el.getBoundingClientRect().height
    if (currentHeight <= maxAutoHeight) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, maxAutoHeight)}px`
    }
  }, [input])

  useEffect(() => {
    const idx = pendingAssistantScrollIdx.current
    if (idx == null) return
    pendingAssistantScrollIdx.current = null
    messageRefs.current[idx]?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  }, [messages.length])

  const clearHistory = () => {
    setMessages([])
    localStorage.removeItem(storageKey)
    const newConversationId = makeConversationId()
    conversationIdRef.current = newConversationId
    try {
      localStorage.setItem(`${storageKey}:conversation_id`, newConversationId)
    } catch {
      // The active conversation still works when browser storage is unavailable.
    }
  }

  const clearStatusTimers = () => {
    statusTimersRef.current.forEach(timer => window.clearTimeout(timer))
    statusTimersRef.current = []
  }

  const startLiveStatus = () => {
    clearStatusTimers()
    statusActiveRef.current = true
    setCurrentTurnId(null)
    answerNowInFlightRef.current = null
    setAnswerNowPending(false)
    setAnswerNowRequestedTurnId(null)
    setAnswerNowError('')
    setLiveStatus(LIVE_STATUS_STEPS[0])
    statusTimersRef.current = [
      window.setTimeout(() => {
        if (statusActiveRef.current) setLiveStatus(LIVE_STATUS_STEPS[1])
      }, 1400),
      window.setTimeout(() => {
        if (statusActiveRef.current) setLiveStatus(LIVE_STATUS_STEPS[2])
      }, 3200),
      window.setTimeout(() => {
        if (statusActiveRef.current) setLiveStatus(LIVE_STATUS_STEPS[3])
      }, 6000),
      window.setTimeout(() => {
        if (statusActiveRef.current) setLiveStatus(LIVE_STATUS_STEPS[4])
      }, 9000),
    ]
  }

  const hideLiveStatus = (options: { clearTurn?: boolean } = {}) => {
    statusActiveRef.current = false
    clearStatusTimers()
    setLiveStatus('')
    if (options.clearTurn) setCurrentTurnId(null)
  }

  const switchLang = (lang: ChatLangCode) => {
    setChatLang(lang)
    if (lang === 'en') return
    // Find the last assistant message
    const lastAssistant = [...messages].reverse().find(m => m.role === 'assistant' && m.content.trim())
    if (!lastAssistant) return
    // Send a hidden translate request — will be shown as a pill in MessageBubble
    const translateText = `${TRANSLATE_PREFIX}${lang}`
    void sendMessage(translateText)
  }

  const sendMessage = async (text: string, baseMessages = messages) => {
    if (!text.trim() || loading) return

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const userMsg: Message = { role: 'user', content: text }
    const updated = [...baseMessages, userMsg]
    setMessages(updated)
    setInput('')
    setEditingMessageIndex(null)
    setLoading(true)
    startLiveStatus()

    const assistantIdx = updated.length
    pendingAssistantScrollIdx.current = assistantIdx
    setMessages(prev => [...prev, { role: 'assistant', content: '' }])

    try {
      const res = await fetch('/api/universal/stream', {
        method: 'POST',
        signal: controller.signal,
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          messages: updated.map(m => ({ role: m.role, content: m.content })),
          ticket_id: ticketId ?? null,
          conversation_id: conversationIdRef.current,
        }),
      })

      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

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
            if (typeof evt.turn_id === 'string' && evt.turn_id) {
              setCurrentTurnId(evt.turn_id)
              setMessages(prev => {
                const next = [...prev]
                next[assistantIdx] = {
                  ...next[assistantIdx],
                  turnId: evt.turn_id,
                }
                return next
              })
            }
            if (typeof evt.status === 'string' && evt.status && statusActiveRef.current) {
              setLiveStatus(evt.status)
            }
            if (evt.delta) {
              hideLiveStatus({ clearTurn: false })
              setMessages(prev => {
                const next = [...prev]
                next[assistantIdx] = {
                  ...next[assistantIdx],
                  content: next[assistantIdx].content + evt.delta,
                }
                return next
              })
            } else if (evt.done) {
              hideLiveStatus({ clearTurn: true })
              setMessages(prev => {
                const next = [...prev]
                next[assistantIdx] = {
                  ...next[assistantIdx],
                  tickets: evt.tickets ?? [],
                  sources: evt.sources ?? [],
                  traceId: evt.trace_id,
                  turnId: evt.turn_id ?? next[assistantIdx].turnId,
                }
                return next
              })
            } else if (evt.error) {
              hideLiveStatus({ clearTurn: true })
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
            // skip malformed
          }
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        hideLiveStatus({ clearTurn: true })
        setMessages(prev => {
          const next = [...prev]
          const existing = next[assistantIdx]?.content?.trim()
          if (existing) return next
          next.splice(assistantIdx, 1)
          return next
        })
        return
      }
      hideLiveStatus({ clearTurn: true })
      setMessages(prev => {
        const next = [...prev]
        next[assistantIdx] = {
          ...next[assistantIdx],
          content: `Error: ${err instanceof Error ? err.message : 'Unknown error'}`,
        }
        return next
      })
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      hideLiveStatus({ clearTurn: true })
      setLoading(false)
    }
  }

  const stopGeneration = () => {
    abortRef.current?.abort()
  }

  const requestAnswerNow = async () => {
    const turnId = currentTurnId
    if (!turnId || answerNowInFlightRef.current === turnId || answerNowRequestedTurnId === turnId) return

    answerNowInFlightRef.current = turnId
    setAnswerNowPending(true)
    setAnswerNowError('')
    try {
      const res = await fetch(`/api/universal/turns/${encodeURIComponent(turnId)}/control`, {
        method: 'POST',
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          action: 'answer_now',
          conversation_id: conversationIdRef.current,
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setAnswerNowRequestedTurnId(turnId)
      clearStatusTimers()
      statusActiveRef.current = true
      setLiveStatus(LIVE_STATUS_STEPS[4])
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error'
      setAnswerNowError(`Answer Now failed: ${message}`)
    } finally {
      if (answerNowInFlightRef.current === turnId) answerNowInFlightRef.current = null
      setAnswerNowPending(false)
    }
  }

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    // Editing intentionally creates a new branch from the edited user turn.
    const baseMessages = editingMessageIndex == null ? messages : messages.slice(0, editingMessageIndex)
    void sendMessage(input, baseMessages)
  }

  const handleInputKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter' || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return
    e.preventDefault()
    // Editing intentionally creates a new branch from the edited user turn.
    const baseMessages = editingMessageIndex == null ? messages : messages.slice(0, editingMessageIndex)
    void sendMessage(input, baseMessages)
  }

  const editMessage = (index: number, text: string) => {
    if (loading) return
    setEditingMessageIndex(index)
    setInput(text)
    window.setTimeout(() => {
      inputRef.current?.focus()
      inputRef.current?.setSelectionRange(text.length, text.length)
    }, 0)
  }

  const improveAnswer = async (index: number) => {
    const traceId = messages[index]?.traceId
    if (!traceId || reviewingIndex != null) return
    setReviewingIndex(index)
    try {
      const res = await fetch('/api/universal/review', {
        method: 'POST',
        headers: await authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ trace_id: traceId }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const review = await res.json() as AnswerReview
      setMessages(prev => {
        const next = [...prev]
        next[index] = { ...next[index], review }
        return next
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error'
      setMessages(prev => {
        const next = [...prev]
        next[index] = {
          ...next[index],
          review: {
            quality: 'weak',
            confidence: 0,
            problem_types: [],
            recommended_action: 'ask_clarification',
            explanation: message,
            user_visible_note: `Review failed: ${message}`,
            improved_answer: null,
          },
        }
        return next
      })
    } finally {
      setReviewingIndex(null)
    }
  }

  const isEmpty = messages.length === 0
  const suggestions = ticketId ? SUGGESTIONS_WITH_TICKET : SUGGESTIONS_NO_TICKET
  const placeholder = ticketId
    ? 'Ask about this ticket, find similar cases, search KB…'
    : 'Ask about tickets, procedures, documents, or trends…'
  const renderInputForm = (placement: 'empty' | 'thread') => (
    <div className={placement === 'thread' ? 'border-t pt-3' : 'w-full'}>
      {editingMessageIndex != null && (
        <div className="mb-2 flex items-center justify-between rounded-md border border-border/70 bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground">
          <span>Editing previous question. Sending will replace the messages after it.</span>
          <button
            type="button"
            className="font-medium text-foreground hover:underline"
            onClick={() => {
              setEditingMessageIndex(null)
              setInput('')
            }}
          >
            Cancel
          </button>
        </div>
      )}
      <form
        onSubmit={handleSubmit}
        className="flex w-full items-end gap-2"
      >
        <textarea
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleInputKeyDown}
          placeholder={placeholder}
          rows={1}
          className="min-h-9 max-h-[min(80vh,36rem)] w-full min-w-0 resize-y overflow-y-auto rounded-md border border-input bg-transparent px-3 py-2 text-sm leading-5 shadow-xs outline-none transition-[color,box-shadow] placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30"
        />
        <Button
          type="submit"
          size="icon"
          variant="default"
          disabled={!input.trim()}
          title="Send"
        >
          <Send className="size-4" />
        </Button>
      </form>
    </div>
  )

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden bg-background">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b shrink-0">
        <div className="flex items-center gap-2">
          <Bot className="size-4 text-primary" />
          <span className="text-sm font-medium">Conversation</span>
          {ticketId && (
            <Badge variant="outline" className="text-xs px-1.5 py-0 h-4 font-mono">
              #{ticketId}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-1">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="h-7 px-2 gap-1 text-xs text-muted-foreground">
                <Languages className="size-3" />
                {chatLang.toUpperCase()}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-36">
              {CHAT_LANGUAGES.map(l => (
                <DropdownMenuItem key={l.code} onClick={() => switchLang(l.code)} className="gap-2">
                  <span className="font-mono text-xs w-6 shrink-0">{l.label}</span>
                  <span className="flex-1">{l.name}</span>
                  {chatLang === l.code && <Check className="size-3 text-primary" />}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          {!isEmpty && (
            <Button variant="ghost" size="sm" onClick={clearHistory} className="h-7 text-xs text-muted-foreground">
              <RotateCcw className="size-3 mr-1" />
              Clear
            </Button>
          )}
          {onExpandToggle && (
            <Button variant="ghost" size="icon" className="size-7" onClick={onExpandToggle} title={expanded ? 'Collapse' : 'Expand to full screen'}>
              {expanded ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
            </Button>
          )}
        </div>
      </div>

      {/* Messages */}
      <ScrollArea className="flex-1 min-h-0 overflow-hidden px-3 py-3">
        {isEmpty ? (
          <div className="space-y-3 pt-2">
            <p className="text-[13px] text-sky-600 dark:text-sky-400 text-center">
              {ticketId
                ? `Ticket #${ticketId} is open as context. Ask anything about it, or ask a general question.`
                : 'Ask anything — past ticket resolutions, procedures, document summaries, or trends.'}
            </p>
            {renderInputForm('empty')}
            <div className="space-y-1">
              {suggestions.map((s, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => void sendMessage(s)}
                  className="w-full text-left text-xs text-muted-foreground hover:text-foreground hover:bg-muted rounded px-2 py-1.5 transition-colors"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {messages.map((msg, i) => (
              <div
                key={i}
                ref={el => { messageRefs.current[i] = el }}
              >
                <MessageBubble
                  msg={msg}
                  index={i}
                  onOpenInPanel={onTicketFound}
                  onImproveAnswer={() => void improveAnswer(i)}
                  onEditMessage={editMessage}
                  reviewing={reviewingIndex === i}
                />
              </div>
            ))}
            {loading && (
              <div className="flex items-center gap-3 text-muted-foreground text-xs" data-turn-id={currentTurnId ?? undefined}>
                {liveStatus && (
                  <span className="inline-flex items-center gap-2">
                    <Loader2 className="size-3 animate-spin" />
                    {liveStatus}
                  </span>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void requestAnswerNow()}
                  disabled={!currentTurnId || answerNowPending || answerNowRequestedTurnId === currentTurnId}
                  className="h-7 gap-1.5 border-cyan-300 bg-cyan-50 px-2.5 text-xs font-medium text-cyan-800 hover:bg-cyan-100 hover:text-cyan-950 disabled:opacity-55 dark:border-cyan-800 dark:bg-cyan-950/30 dark:text-cyan-200 dark:hover:bg-cyan-900/50"
                  title={currentTurnId ? 'Finish with the best available answer' : 'Waiting for turn id'}
                >
                  {answerNowPending ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
                  {answerNowRequestedTurnId === currentTurnId ? 'Answering now' : answerNowPending ? 'Answering...' : 'Answer Now'}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={stopGeneration}
                  className="h-7 gap-1.5 border-slate-300 bg-slate-50 px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-100 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-900/50 dark:text-slate-200 dark:hover:bg-slate-800"
                  title="Stop response"
                >
                  <Square className="size-3.5 fill-current" />
                  Stop response
                </Button>
                {answerNowError && (
                  <span className="text-[11px] text-amber-700 dark:text-amber-300">{answerNowError}</span>
                )}
              </div>
            )}
            <div className="pb-3">
              {renderInputForm('thread')}
            </div>
          </div>
        )}
      </ScrollArea>
    </div>
  )
}
