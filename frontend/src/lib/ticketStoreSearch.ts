/**
 * Unified ticket vs store number parsing and routing plan (no I/O).
 * Store numbers are 4 digits; ticket IDs can overlap (e.g. legacy #1048).
 */

export type LookupPlan =
  | { kind: 'invalid'; message: string }
  | { kind: 'explicit_store'; storeId: string; historyQuery: string }
  | { kind: 'ticket_ids'; ids: number[]; truncated: boolean; historyQuery: string }
  /** Isolated 4-digit store token inside broader text (e.g. pasted line with store number). */
  | { kind: 'store_from_text'; storeId: string; hasMultiple: boolean; historyQuery: string }

/** Parse up to maxCount ticket IDs from raw input. */
export function parseTicketIds(raw: string, maxCount = 4): { ids: number[]; truncated: boolean } {
  const tokens = raw.split(/[\s,;:|]+/).filter(Boolean)
  const ids: number[] = []

  for (const token of tokens) {
    const clean = token.replace(/^#+/, '').trim()

    const rangeMatch = clean.match(/^(\d+)-(\d+)$/)
    if (rangeMatch) {
      const start = parseInt(rangeMatch[1], 10)
      const end = parseInt(rangeMatch[2], 10)
      if (!isNaN(start) && !isNaN(end) && start > 0) {
        const lo = Math.min(start, end)
        const hi = Math.max(start, end)
        const rangeSize = Math.min(hi - lo + 1, 20)
        for (let i = lo; i < lo + rangeSize; i++) ids.push(i)
      }
      continue
    }

    const numStr = clean.replace(/^\D+/, '').replace(/\D+$/, '')
    const n = parseInt(numStr, 10)
    if (!isNaN(n) && n > 0) ids.push(n)
  }

  const unique = [...new Set(ids)]
  return {
    ids: unique.slice(0, maxCount),
    truncated: unique.length > maxCount,
  }
}

/** First isolated 4-digit store token; not part of a longer digit run. */
export function parseStoreNumbers(raw: string): { storeId: string | null; hasMultiple: boolean } {
  const matches = raw.match(/(?<!\d)\d{4}(?!\d)/g) ?? []
  return {
    storeId: matches[0] ?? null,
    hasMultiple: matches.length > 1,
  }
}

const EXPLICIT_STORE_COLON = /^store:\s*(\d{4})\s*$/i
const EXPLICIT_STORE_S = /^s(\d{4})\s*$/i

/**
 * Decide how to resolve a single search box submission.
 */
export function planLookup(raw: string): LookupPlan {
  const trimmed = raw.trim()
  if (!trimmed) {
    return { kind: 'invalid', message: 'Enter a ticket number, store number, or search text.' }
  }

  const storeColon = trimmed.match(EXPLICIT_STORE_COLON)
  if (storeColon) {
    return { kind: 'explicit_store', storeId: storeColon[1], historyQuery: trimmed }
  }
  const storeS = trimmed.match(EXPLICIT_STORE_S)
  if (storeS) {
    return { kind: 'explicit_store', storeId: storeS[1], historyQuery: trimmed }
  }

  if (/^\s*#/.test(trimmed)) {
    const { ids, truncated } = parseTicketIds(trimmed)
    if (ids.length === 0) {
      return { kind: 'invalid', message: 'Enter a valid ticket number after # (e.g. #950014).' }
    }
    return { kind: 'ticket_ids', ids, truncated, historyQuery: trimmed }
  }

  if (/^\d{4}$/.test(trimmed)) {
    return { kind: 'explicit_store', storeId: trimmed, historyQuery: trimmed }
  }

  if (/^\d{5,}$/.test(trimmed)) {
    return {
      kind: 'ticket_ids',
      ids: [parseInt(trimmed, 10)],
      truncated: false,
      historyQuery: trimmed,
    }
  }

  if (/^\d{1,3}$/.test(trimmed)) {
    return {
      kind: 'invalid',
      message: `Use #${trimmed} to open a legacy ticket. Store numbers use four digits.`,
    }
  }

  const { storeId, hasMultiple } = parseStoreNumbers(trimmed)
  if (storeId) {
    return {
      kind: 'store_from_text',
      storeId,
      hasMultiple,
      historyQuery: trimmed,
    }
  }

  const ticketParsed = parseTicketIds(trimmed)
  if (ticketParsed.ids.length > 0) {
    return {
      kind: 'ticket_ids',
      ids: ticketParsed.ids,
      truncated: ticketParsed.truncated,
      historyQuery: trimmed,
    }
  }

  return {
    kind: 'invalid',
    message:
      'Enter a ticket (#950014 or 950014-950016), a 4-digit store (or store:2002), or paste text containing a store number.',
  }
}
