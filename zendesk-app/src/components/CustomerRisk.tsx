import { useEffect, useState } from 'react'
import type { AppContext } from '../App'
import { trackZafImpactEvent } from '../impactTelemetry'
import InlineMarkdownText from './InlineMarkdownText'

interface Props {
  ctx: AppContext
  backend: string
  compact?: boolean
}

interface CustomerRiskResponse {
  severity: 'green' | 'yellow' | 'orange' | 'red'
  score: number
  label: string
  tone: 'calm' | 'concerned' | 'frustrated' | 'escalated'
  summary: string
  reasons: string[]
  next_action: string
  from_cache: boolean
}

export default function CustomerRisk({ ctx, backend, compact = false }: Props) {
  const [risk, setRisk] = useState<CustomerRiskResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false

    async function load() {
      setLoading(true)
      setError('')
      try {
        const resp = await fetch(`${backend}/api/tickets/${ctx.ticketId}/customer-risk`, {
          headers: { Authorization: `Bearer ${ctx.sessionToken}` },
        })
        if (!resp.ok) {
          if (!cancelled) setError('Customer Risk unavailable.')
          return
        }
        const data = await resp.json() as CustomerRiskResponse
        if (!cancelled) setRisk(data)
        void trackZafImpactEvent(backend, ctx.sessionToken, {
          event_type: 'customer_risk_viewed',
          surface: compact ? 'zaf_sidebar' : 'zaf_modal',
          workflow: 'Customer Risk',
          ticket_id: ctx.ticketId,
          object_type: 'customer_risk',
          object_id: String(ctx.ticketId),
          metadata: { severity: data.severity, from_cache: data.from_cache },
        })
      } catch {
        if (!cancelled) setError('Customer Risk unavailable.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void load()
    return () => { cancelled = true }
  }, [ctx.ticketId, ctx.sessionToken, backend])

  if (loading) {
    return (
      <div className="section">
        <div className="loading">Assessing customer risk...</div>
      </div>
    )
  }

  if (error && !risk) {
    return (
      <div className="section">
        <div className="error-msg">{error}</div>
      </div>
    )
  }

  if (!risk) return null

  return (
    <div className="section">
      <div className={`risk-card risk-card--${risk.severity}`}>
        <div className="risk-head">
          <div>
            <div className="risk-kicker">Customer Risk</div>
            <div className="risk-label">{risk.label}</div>
          </div>
          <span className="risk-badge">{risk.severity}</span>
        </div>
        <InlineMarkdownText as="div" className="risk-summary" text={risk.summary} />
        {!compact && risk.reasons.length > 0 && (
          <ul className="risk-reasons">
            {risk.reasons.slice(0, 3).map(reason => (
              <li key={reason}>
                <InlineMarkdownText text={reason} />
              </li>
            ))}
          </ul>
        )}
        <div className="risk-next">
          <strong>Next:</strong> <InlineMarkdownText text={risk.next_action} />
        </div>
      </div>
    </div>
  )
}
