import { useEffect, useState } from 'react'
import type { AppContext } from '../App'
import { trackZafImpactEvent } from '../impactTelemetry'
import InlineMarkdownText from './InlineMarkdownText'

interface Props {
  ctx: AppContext
  backend: string
}

interface KBArticle {
  title: string
  url: string
  snippet: string
}

interface BriefSummary {
  overview?: string
  current_status?: string
  key_actions_taken?: string[]
  kb_suggestions?: KBArticle[]
}

export default function ModalSummary({ ctx, backend }: Props) {
  const [summary, setSummary] = useState<BriefSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false

    async function load() {
      setLoading(true)
      setSummary(null)
      setError('')
      try {
        const resp = await fetch(`${backend}/api/ai/summary`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${ctx.sessionToken}`,
          },
          body: JSON.stringify({ ticket_id: ctx.ticketId, mode: 'brief' }),
        })
        if (!resp.ok) {
          if (!cancelled) setError('Could not load summary.')
          return
        }
        const data = await resp.json() as { summary?: BriefSummary | string }
        void trackZafImpactEvent(backend, ctx.sessionToken, {
          event_type: 'summary_viewed',
          surface: 'zaf_modal',
          workflow: 'Summary',
          ticket_id: ctx.ticketId,
          object_type: 'ticket_summary',
          object_id: `${ctx.ticketId}:brief`,
        })
        const s = data.summary
        if (typeof s === 'string') {
          if (!cancelled) setSummary({ overview: s })
        } else if (s) {
          if (!cancelled) setSummary(s)
        } else {
          if (!cancelled) setError('No summary available.')
        }
      } catch {
        if (!cancelled) setError('Could not load summary.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => { cancelled = true }
  }, [ctx.ticketId, ctx.sessionToken, backend])

  if (loading) return <div className="loading">Generating summary…</div>
  if (error) return <div className="error-msg">{error}</div>
  if (!summary) return null

  return (
    <div className="modal-summary">
      {summary.overview && (
        <div className="modal-summary-block">
          <div className="detail-label" style={{ marginBottom: 6 }}>Overview</div>
          <InlineMarkdownText as="div" className="summary-text" text={summary.overview} />
        </div>
      )}

      {summary.current_status && (
        <div className="modal-summary-block">
          <div className="detail-label" style={{ marginBottom: 6 }}>Current Status</div>
          <InlineMarkdownText as="div" className="summary-text" text={summary.current_status} />
        </div>
      )}

      {summary.key_actions_taken && summary.key_actions_taken.length > 0 && (
        <div className="modal-summary-block">
          <div className="detail-label" style={{ marginBottom: 6 }}>Actions Taken</div>
          <ul className="actions-list">
            {summary.key_actions_taken.map((a, i) => (
              <li key={i}>
                <InlineMarkdownText text={a} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {summary.kb_suggestions && summary.kb_suggestions.length > 0 && (
        <div className="modal-summary-block">
          <div className="detail-label" style={{ marginBottom: 6 }}>Related KB Articles</div>
          {summary.kb_suggestions.map((a, i) => (
            <a
              key={i}
              className="kb-link"
              href={a.url}
              target="_blank"
              rel="noreferrer"
              title={a.snippet}
              onClick={() => void trackZafImpactEvent(backend, ctx.sessionToken, {
                event_type: 'document_link_clicked',
                surface: 'zaf_modal',
                workflow: 'Source clicks',
                ticket_id: ctx.ticketId,
                object_type: 'kb_article',
                object_id: a.url,
                object_label: a.title,
                target_url: a.url,
              })}
            >
              {a.title}
            </a>
          ))}
        </div>
      )}
    </div>
  )
}
