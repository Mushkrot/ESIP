import { useState } from 'react'
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

export default function Summary({ ctx, backend }: Props) {
  const [summary, setSummary] = useState<BriefSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [collapsed, setCollapsed] = useState(false)

  async function load(refresh = false) {
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
        body: JSON.stringify({ ticket_id: ctx.ticketId, mode: 'brief', refresh }),
      })
      if (!resp.ok) {
        setError('Could not load summary.')
        return
      }
      const data = await resp.json() as { summary?: BriefSummary | string }
      void trackZafImpactEvent(backend, ctx.sessionToken, {
        event_type: refresh ? 'summary_generated' : 'summary_viewed',
        surface: 'zaf_sidebar',
        workflow: 'Summary',
        ticket_id: ctx.ticketId,
        object_type: 'ticket_summary',
        object_id: `${ctx.ticketId}:brief`,
        metadata: { refresh },
      })
      const s = data.summary
      if (typeof s === 'string') {
        setSummary({ overview: s })
      } else if (s) {
        setSummary(s)
      } else {
        setError('No summary available.')
      }
    } catch {
      setError('Could not load summary.')
    } finally {
      setLoading(false)
    }
  }

  const hasDetails = summary && (summary.current_status || (summary.key_actions_taken?.length ?? 0) > 0)

  return (
    <div className="section">
      <div className="section-header" onClick={() => setCollapsed(c => !c)}>
        <span className="section-title">AI Summary</span>
        <span className="section-toggle">{collapsed ? '▼' : '▲'}</span>
      </div>

      {!collapsed && (
        <>
          {!loading && !summary && !error && (
            <button className="esip-btn esip-btn--secondary" onClick={() => load()}>
              Generate Summary
            </button>
          )}
          {loading && <div className="loading">Generating summary…</div>}
          {error && <div className="error-msg">{error}</div>}

          {summary && (
            <>
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 4 }}>
                <button className="expand-btn" onClick={() => load(true)}>↺ Refresh</button>
              </div>
              {/* Overview — always visible */}
              {summary.overview && (
                <InlineMarkdownText as="div" className="summary-text" text={summary.overview} />
              )}

              {/* Expandable details */}
              {hasDetails && (
                <>
                  {expanded && (
                    <div style={{ marginTop: 8 }}>
                      {summary.current_status && (
                        <div style={{ marginBottom: 6 }}>
                          <span className="detail-label">Status: </span>
                          <InlineMarkdownText className="summary-text" text={summary.current_status} />
                        </div>
                      )}
                      {summary.key_actions_taken && summary.key_actions_taken.length > 0 && (
                        <div>
                          <span className="detail-label">Actions taken:</span>
                          <ul className="actions-list">
                            {summary.key_actions_taken.map((a, i) => (
                              <li key={i}>
                                <InlineMarkdownText text={a} />
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                  <button className="expand-btn" onClick={() => setExpanded(e => !e)}>
                    {expanded ? 'Show less ▲' : 'Show more ▼'}
                  </button>
                </>
              )}

              {/* KB Suggestions */}
              {summary.kb_suggestions && summary.kb_suggestions.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div className="detail-label" style={{ marginBottom: 4 }}>Related articles:</div>
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
                        surface: 'zaf_sidebar',
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
            </>
          )}
        </>
      )}
    </div>
  )
}
