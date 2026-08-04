import { useEffect, useState } from 'react'
import type { AppContext } from '../App'
import { trackZafImpactEvent } from '../impactTelemetry'

interface Props {
  ctx: AppContext
  backend: string
}

interface Ticket {
  id: number
  subject: string
  status: string
  org_name: string
  solved_at?: string
}

export default function SimilarTickets({ ctx, backend }: Props) {
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [ticketBaseUrl, setTicketBaseUrl] = useState('/agent/tickets')

  async function load() {
    setLoading(true)
    setTickets([])
    try {
      const resp = await fetch(
        `${backend}/api/tickets/${ctx.ticketId}/similar?limit=5`,
        { headers: { 'Authorization': `Bearer ${ctx.sessionToken}` } }
      )
      if (!resp.ok) return
      const data = await resp.json() as { tickets?: Ticket[] }
      setTickets(data.tickets ?? [])
      void trackZafImpactEvent(backend, ctx.sessionToken, {
        event_type: 'similar_tickets_viewed',
        surface: 'zaf_sidebar',
        workflow: 'Similar cases',
        ticket_id: ctx.ticketId,
        metadata: { count: data.tickets?.length ?? 0, ticket_ids: (data.tickets ?? []).map(t => t.id) },
      })
    } catch {
      // silently fail — similar tickets are supplementary
    } finally {
      setLoading(false)
      setLoaded(true)
    }
  }

  function openTicket(id: number) {
    void trackZafImpactEvent(backend, ctx.sessionToken, {
      event_type: 'similar_ticket_opened',
      surface: 'zaf_sidebar',
      workflow: 'Similar cases',
      ticket_id: ctx.ticketId,
      target_ticket_id: id,
      object_type: 'ticket',
      object_id: String(id),
      target_url: `${ticketBaseUrl}/${id}`,
    })
    const newTab = window.open(`${ticketBaseUrl}/${id}`, '_blank', 'noopener,noreferrer')
    if (!newTab) {
      // Fallback if popup is blocked by browser policy.
      ctx.client.invoke('routeTo', 'ticket', String(id))
    }
  }

  useEffect(() => {
    let cancelled = false
    ctx.client.get('currentAccount.subdomain')
      .then((data: Record<string, unknown>) => {
        const subdomain = data['currentAccount.subdomain']
        if (!cancelled && typeof subdomain === 'string' && subdomain.trim()) {
          setTicketBaseUrl(`https://${subdomain}.zendesk.com/agent/tickets`)
        }
      })
      .catch(() => {
        // Keep relative fallback URL.
      })
    return () => { cancelled = true }
  }, [ctx.client])

  return (
    <div className="section">
      <div className="section-header" onClick={() => setCollapsed(c => !c)}>
        <span className="section-title">Similar Tickets</span>
        <span className="section-toggle">{collapsed ? '▼' : '▲'}</span>
      </div>
      {!collapsed && (
        !loading && !loaded
          ? <button className="esip-btn esip-btn--secondary" onClick={load}>Find Similar Tickets</button>
          : loading
          ? <div className="loading">Searching similar tickets…</div>
          : tickets.length === 0
            ? <div className="loading">No similar tickets found.</div>
            : tickets.map(t => (
              <div key={t.id} className="ticket-card" onClick={() => openTicket(t.id)}>
                <div className="ticket-id">
                  #{t.id}
                  <span className={`ticket-status ticket-status--${t.status}`}>{t.status}</span>
                </div>
                <div className="ticket-subject">{t.subject}</div>
                <div className="ticket-meta">
                  {t.org_name}{t.solved_at ? ` · ${t.solved_at.slice(0, 10)}` : ''}
                </div>
              </div>
            ))
      )}
    </div>
  )
}
