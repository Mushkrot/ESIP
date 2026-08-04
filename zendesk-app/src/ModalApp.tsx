import { Component, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { createReferenceZafClient } from './App'
import type { AppContext } from './App'
import CustomerRisk from './components/CustomerRisk'
import ModalSummary from './components/ModalSummary'
import ModalSimilarTickets from './components/ModalSimilarTickets'
import ModalChat from './components/ModalChat'
import { BACKEND_URL, platformTicketUrl } from './config'
import { trackZafImpactEvent } from './impactTelemetry'
import esipMark from './assets/esip-mark.svg'

interface EBState { error: string | null }

class ErrorBoundary extends Component<{ children: ReactNode }, EBState> {
  state: EBState = { error: null }
  static getDerivedStateFromError(err: unknown): EBState {
    return { error: err instanceof Error ? err.message : String(err) }
  }
  render() {
    if (this.state.error) return <div className="auth-error">Render error: {this.state.error}</div>
    return this.props.children
  }
}

function ModalInner() {
  const [ctx, setCtx] = useState<AppContext | null>(null)
  const [authError, setAuthError] = useState('')
  const [status, setStatus] = useState('Initializing…')
  const [activeTab, setActiveTab] = useState<'summary' | 'similar'>('summary')

  useEffect(() => {
    async function init() {
      try {
        const localReference = window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost'
        if (!window.ZAFClient && !localReference) {
          setAuthError('ESIP: Zendesk SDK not available.')
          return
        }

        const client = localReference ? createReferenceZafClient() : window.ZAFClient.init()

        // Resize modal
        client.invoke('resize', { width: '920px', height: '640px' })

        // Read ticketId from URL params
        const params = new URLSearchParams(window.location.search)
        const ticketId = parseInt(params.get('ticketId') ?? '', 10)
        if (!ticketId) {
          setAuthError('ESIP: no ticket ID in URL.')
          return
        }

        // Get current user from ZAF
        setStatus('Authenticating…')
        const userData = await client.get('currentUser')
        const currentUser = userData['currentUser'] as Record<string, unknown> | null
        const email = (currentUser?.['email'] as string) ?? ''
        const name = (currentUser?.['name'] as string) ?? email
        const role = (currentUser?.['role'] as string) ?? 'agent'

        if (!email) {
          setAuthError('ESIP: no email from Zendesk.')
          return
        }

        const authResp = await fetch(`${BACKEND_URL}/api/zaf/auth`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, name, role }),
        })

        if (!authResp.ok) {
          const err = await authResp.json().catch(() => ({ detail: `HTTP ${authResp.status}` }))
          setAuthError(`ESIP: ${(err as { detail?: string }).detail ?? 'Auth failed'}`)
          return
        }

        const { session_token } = await authResp.json() as { session_token: string }
        if (!session_token) {
          setAuthError('ESIP: no session token returned.')
          return
        }

        setCtx({ ticketId, sessionToken: session_token, client })
        void trackZafImpactEvent(BACKEND_URL, session_token, {
          event_type: 'zaf_modal_opened',
          surface: 'zaf_modal',
          workflow: 'Zendesk app',
          ticket_id: ticketId,
          object_type: 'ticket',
          object_id: String(ticketId),
        })

      } catch (err) {
        const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err)
        setAuthError(`ESIP error: ${msg || 'unknown'}`)
      }
    }

    void init()
  }, [])

  if (authError) return <div className="auth-error">{authError}</div>
  if (!ctx) return <div className="modal-loading">{status}</div>

  return (
    <div className="modal-layout">
      {/* Left panel */}
      <div className="modal-left">
        <div className="modal-tabs">
          <button
            className={`modal-tab${activeTab === 'summary' ? ' modal-tab--active' : ''}`}
            onClick={() => setActiveTab('summary')}
          >
            AI Summary
          </button>
          <button
            className={`modal-tab${activeTab === 'similar' ? ' modal-tab--active' : ''}`}
            onClick={() => setActiveTab('similar')}
          >
            Similar Tickets
          </button>
        </div>
        <div className="modal-left-content">
          {activeTab === 'summary'
            ? <>
                <CustomerRisk ctx={ctx} backend={BACKEND_URL} />
                <ModalSummary ctx={ctx} backend={BACKEND_URL} />
              </>
            : <ModalSimilarTickets ctx={ctx} backend={BACKEND_URL} />
          }
        </div>
      </div>

      {/* Right panel — Chat */}
      <div className="modal-right">
        <div className="modal-right-header">
          <div className="modal-brand-lockup">
            <img src={esipMark} alt="" />
            <span className="section-title">ESIP Chat — Ticket #{ctx.ticketId}</span>
          </div>
        </div>
        <ModalChat ctx={ctx} backend={BACKEND_URL} />
        <div className="modal-esip-row">
          <button
            className="esip-btn"
            onClick={() => {
              void trackZafImpactEvent(BACKEND_URL, ctx.sessionToken, {
                event_type: 'ticket_link_clicked',
                surface: 'zaf_modal',
                workflow: 'Source clicks',
                ticket_id: ctx.ticketId,
                object_type: 'ticket',
                object_id: String(ctx.ticketId),
                target_url: platformTicketUrl(ctx.ticketId),
              })
              window.open(platformTicketUrl(ctx.ticketId), '_blank', 'noopener,noreferrer')
            }}
          >
            Open in ESIP →
          </button>
          <div className="esip-hint">Full platform: analytics, search, KB gap</div>
        </div>
      </div>
    </div>
  )
}

export default function ModalApp() {
  return (
    <ErrorBoundary>
      <ModalInner />
    </ErrorBoundary>
  )
}
