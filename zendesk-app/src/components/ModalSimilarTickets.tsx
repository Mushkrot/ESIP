import { useEffect, useState } from 'react'
import type { AppContext } from '../App'
import { trackZafImpactEvent } from '../impactTelemetry'
import InlineMarkdownText from './InlineMarkdownText'

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
  resolution?: string
}

export default function ModalSimilarTickets({ ctx, backend }: Props) {
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [loading, setLoading] = useState(true)
  const [ticketBaseUrl, setTicketBaseUrl] = useState('/agent/tickets')

  useEffect(() => {
    let cancelled = false

    async function load() {
      setLoading(true)
      setTickets([])
      try {
        const resp = await fetch(
          `${backend}/api/tickets/${ctx.ticketId}/similar?limit=10`,
          { headers: { 'Authorization': `Bearer ${ctx.sessionToken}` } }
        )
        if (!resp.ok) return
        const data = await resp.json() as { tickets?: Ticket[] }
        if (!cancelled) setTickets(data.tickets ?? [])
        void trackZafImpactEvent(backend, ctx.sessionToken, {
          event_type: 'similar_tickets_viewed',
          surface: 'zaf_modal',
          workflow: 'Similar cases',
          ticket_id: ctx.ticketId,
          metadata: { count: data.tickets?.length ?? 0, ticket_ids: (data.tickets ?? []).map(t => t.id) },
        })
      } catch {
        // silently fail
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => { cancelled = true }
  }, [ctx.ticketId, ctx.sessionToken, backend])

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

  function openTicket(id: number) {
    void trackZafImpactEvent(backend, ctx.sessionToken, {
      event_type: 'similar_ticket_opened',
      surface: 'zaf_modal',
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

  if (loading) return <div className="loading">Searching similar tickets…</div>
  if (tickets.length === 0) return <div className="loading">No similar tickets found.</div>

  return (
    <div className="modal-similar">
      {tickets.map(t => (
        <div key={t.id} className="ticket-card modal-ticket-card" onClick={() => openTicket(t.id)}>
          <div className="ticket-id">
            #{t.id}
            <span className={`ticket-status ticket-status--${t.status}`}>{t.status}</span>
          </div>
          <div className="ticket-subject">{t.subject}</div>
          <div className="ticket-meta">
            {t.org_name}{t.solved_at ? ` · ${t.solved_at.slice(0, 10)}` : ''}
          </div>
          {t.resolution && (
            <InlineMarkdownText as="div" className="modal-ticket-resolution" text={t.resolution} />
          )}
        </div>
      ))}
    </div>
  )
}
