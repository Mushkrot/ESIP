/**
 * chatStorage — localStorage-based persistence for ESIP analytics chat.
 *
 * - Analytics messages + filters persist under legacy esip_reference_* keys.
 * - TTL: 7 days. On load, stale keys are deleted and empty state is returned.
 * - Max 30 messages per session (oldest trimmed on save).
 */

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

export interface AnalyticsMessage {
  role: 'user' | 'assistant'
  content: string
  sql?: string | null
  rowCount?: number | null
  table?: {
    columns: string[]
    rows: (string | number | boolean | null)[][]
  } | null
  kbResults?: {
    title: string
    url: string
    snippet: string
  }[] | null
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TTL_MS = 7 * 24 * 60 * 60 * 1000   // 7 days
const MAX_MESSAGES = 30
const KEY_ANALYTICS_MESSAGES = 'esip_reference_messages'
const KEY_ANALYTICS_FILTERS = 'esip_reference_filters'

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function isExpired(savedAt: string): boolean {
  return Date.now() - new Date(savedAt).getTime() > TTL_MS
}

function safeRead<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

function safeWrite(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Quota exceeded or private-mode restrictions — fail silently
  }
}

// ---------------------------------------------------------------------------
// Analytics messages
// ---------------------------------------------------------------------------

export function loadAnalyticsMessages(): AnalyticsMessage[] {
  const stored = safeRead<{ messages: AnalyticsMessage[]; savedAt: string }>(KEY_ANALYTICS_MESSAGES)
  if (!stored) return []
  if (isExpired(stored.savedAt)) {
    localStorage.removeItem(KEY_ANALYTICS_MESSAGES)
    return []
  }
  return stored.messages
}

export function saveAnalyticsMessages(messages: AnalyticsMessage[]): void {
  const trimmed = messages.slice(-MAX_MESSAGES)
  safeWrite(KEY_ANALYTICS_MESSAGES, { messages: trimmed, savedAt: new Date().toISOString() })
}

export function clearAnalyticsMessages(): void {
  localStorage.removeItem(KEY_ANALYTICS_MESSAGES)
}

// ---------------------------------------------------------------------------
// Analytics filters
// ---------------------------------------------------------------------------

export function loadAnalyticsFilters(): { timeframe: string; brand: string } {
  const stored = safeRead<{ timeframe: string; brand: string; savedAt: string }>(KEY_ANALYTICS_FILTERS)
  if (!stored) return { timeframe: '365', brand: '' }
  if (isExpired(stored.savedAt)) {
    localStorage.removeItem(KEY_ANALYTICS_FILTERS)
    return { timeframe: '365', brand: '' }
  }
  return { timeframe: stored.timeframe, brand: stored.brand }
}

export function saveAnalyticsFilters(timeframe: string, brand: string): void {
  safeWrite(KEY_ANALYTICS_FILTERS, { timeframe, brand, savedAt: new Date().toISOString() })
}
