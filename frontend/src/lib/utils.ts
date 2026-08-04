import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// ---------------------------------------------------------------------------
// Zendesk slug → human-readable title
// Mirrors backend bigquery.py _slug_to_title()
// ---------------------------------------------------------------------------

const UPPERCASE_CODES = new Set([
  'kds', 'pos', 'dmb', 'ups', 'msp', 'api', 'dt', 'ids',
])

const SLUG_PREFIXES = ['rootcause_', 'zd_', 'source_', 'rc_']

function fmtWord(w: string): string {
  return UPPERCASE_CODES.has(w.toLowerCase()) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)
}

export function slugToTitle(slug: string): string {
  if (!slug) return slug

  // Strip known prefixes
  for (const prefix of SLUG_PREFIXES) {
    if (slug.toLowerCase().startsWith(prefix)) {
      slug = slug.slice(prefix.length)
      break
    }
  }

  // Hierarchical: split on double underscore → " > "
  if (slug.includes('__')) {
    return slug.split('__').filter(Boolean).map(slugToTitle).join(' > ')
  }

  // Preserve " - " separators (Zendesk _-_ grouping)
  slug = slug.replace(/_-_+/g, ' - ')
  // Split remaining hyphens (but not those already surrounded by spaces)
  slug = slug.replace(/(?<! )-(?! )/g, ' ')
  // Collapse underscores + extra whitespace
  const result = slug.replace(/[_\s]+/g, ' ').trim()

  // Handle slash-separated parts (e.g. "outage/failure")
  return result
    .split('/')
    .map(part => part.split(' ').map(fmtWord).join(' '))
    .join('/')
}
