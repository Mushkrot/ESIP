import { Component, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import CustomerRisk from './components/CustomerRisk'
import Summary from './components/Summary'
import SimilarTickets from './components/SimilarTickets'
import { BACKEND_URL, platformTicketUrl } from './config'
import { trackZafImpactEvent } from './impactTelemetry'
import esipMark from './assets/esip-mark.svg'

declare global {
  interface Window {
    ZAFClient: {
      init: () => ZAFClientInstance
    }
  }
}

export interface ZAFClientInstance {
  get: (path: string | string[]) => Promise<Record<string, unknown>>
  invoke: (cmd: string, ...args: unknown[]) => void
  on: (event: string, cb: (data: unknown) => void) => void
  metadata: () => Promise<{ settings: Record<string, string> }>
}

function isLocalReferenceHost(): boolean {
  return window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost'
}

export function createReferenceZafClient(): ZAFClientInstance {
  return {
    async get(path: string | string[]) {
      const keys = Array.isArray(path) ? path : [path]
      const values: Record<string, unknown> = {}
      for (const key of keys) {
        if (key === 'ticket.id') values[key] = 950001
        if (key === 'currentUser') {
          values[key] = {
            email: 'alex.morgan@northstar.example.com',
            name: 'Alex Morgan',
            role: 'reference',
          }
        }
        if (key === 'currentAccount.subdomain') values[key] = ''
      }
      return values
    },
    invoke(command: string, ...args: unknown[]) {
      if (command === 'instances.create' && isLocalReferenceHost()) {
        const options = (args[0] || {}) as { url?: string }
        const query = options.url?.split('?')[1] || 'ticketId=950001'
        window.open(`/modal.html?${query}`, '_blank', 'noopener,noreferrer')
      }
    },
    on() {},
    async metadata() { return { settings: {} } },
  }
}

export interface AppContext {
  ticketId: number
  sessionToken: string
  client: ZAFClientInstance
}

// ---------------------------------------------------------------------------
// Error boundary
// ---------------------------------------------------------------------------

interface EBState { error: string | null }

class ErrorBoundary extends Component<{ children: ReactNode }, EBState> {
  state: EBState = { error: null }

  static getDerivedStateFromError(err: unknown): EBState {
    const msg = err instanceof Error ? err.message : String(err)
    return { error: msg }
  }

  render() {
    if (this.state.error) {
      return <div className="auth-error">Render error: {this.state.error}</div>
    }
    return this.props.children
  }
}

// ---------------------------------------------------------------------------
// Main app
// ---------------------------------------------------------------------------

function AppInner() {
  const [ctx, setCtx] = useState<AppContext | null>(null)
  const [authError, setAuthError] = useState('')
  const [status, setStatus] = useState('Initializing…')

  useEffect(() => {
    async function init() {
      try {
        if (!window.ZAFClient && !isLocalReferenceHost()) {
          setAuthError('ESIP: Zendesk SDK not available.')
          return
        }

        const client = isLocalReferenceHost() ? createReferenceZafClient() : window.ZAFClient.init()
        client.invoke('resize', { width: '320px', height: '600px' })

        setStatus('Loading ticket info…')
        const ticketData = await client.get('ticket.id')
        const ticketId = ticketData['ticket.id'] as number

        if (!ticketId) {
          setAuthError('ESIP: could not get ticket ID.')
          return
        }

        setStatus('Loading user info…')
        const userData = await client.get('currentUser')
        const currentUser = userData['currentUser'] as Record<string, unknown> | null

        const email = (currentUser?.['email'] as string) ?? ''
        const name = (currentUser?.['name'] as string) ?? email
        const role = (currentUser?.['role'] as string) ?? 'agent'

        if (!email) {
          setAuthError(`ESIP: no email from Zendesk. Got: ${JSON.stringify(currentUser)}`)
          return
        }

        setStatus('Authenticating…')
        const authResp = await fetch(`${BACKEND_URL}/api/zaf/auth`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, name, role }),
        })

        if (!authResp.ok) {
          if (authResp.status === 403) {
            setAuthError('not_provisioned')
          } else if (authResp.status === 503) {
            setAuthError('maintenance')
          } else {
            setAuthError('error')
          }
          return
        }

        const { session_token } = await authResp.json() as { session_token: string }

        if (!session_token) {
          setAuthError('ESIP: no session token returned.')
          return
        }

        setCtx({ ticketId, sessionToken: session_token, client })
        void trackZafImpactEvent(BACKEND_URL, session_token, {
          event_type: 'zaf_sidebar_opened',
          surface: 'zaf_sidebar',
          workflow: 'Zendesk app',
          ticket_id: ticketId,
          object_type: 'ticket',
          object_id: String(ticketId),
        })
        setTimeout(() => client.invoke('resize', { width: '320px', height: '700px' }), 1000)

      } catch (err) {
        const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err)
        setAuthError(`ESIP error: ${msg || 'unknown'}`)
      }
    }

    void init()
  }, [])

  function openModal() {
    if (!ctx) return
    void trackZafImpactEvent(BACKEND_URL, ctx.sessionToken, {
      event_type: 'zaf_modal_opened',
      surface: 'zaf_sidebar',
      workflow: 'Zendesk app',
      ticket_id: ctx.ticketId,
      object_type: 'ticket',
      object_id: String(ctx.ticketId),
    })
    ctx.client.invoke('instances.create', {
      location: 'modal',
      url: `assets/modal.html?ticketId=${ctx.ticketId}`,
    })
  }

  if (authError === 'not_provisioned') {
    return (
      <div className="auth-error">
        <div style={{ fontWeight: 600, marginBottom: 4 }}>ESIP is not available for your account.</div>
        <div style={{ fontSize: 12, color: '#666' }}>
          Contact IT if you need access. If your access was just added, click Refresh to try again.
        </div>
        <button
          type="button"
          className="esip-btn esip-btn--secondary"
          style={{ marginTop: 8 }}
          onClick={() => window.location.reload()}
        >
          Refresh
        </button>
      </div>
    )
  }

  if (authError === 'maintenance') {
    return (
      <div className="auth-error">
        <div style={{ fontWeight: 600, marginBottom: 4 }}>ESIP is in maintenance mode.</div>
        <div style={{ fontSize: 12, color: '#666' }}>Only administrators can sign in right now.</div>
      </div>
    )
  }

  if (authError) {
    return <div className="auth-error">ESIP could not load. Please refresh the page.</div>
  }

  if (!ctx) {
    return <div className="section loading">{status}</div>
  }

  return (
    <div className="esip-companion">
      <header className="esip-companion-header">
        <img src={esipMark} alt="" />
        <div>
          <strong>ESIP</strong>
          <span>Ticket intelligence</span>
        </div>
        <small>Context attached</small>
      </header>
      <CustomerRisk ctx={ctx} backend={BACKEND_URL} compact />
      <Summary ctx={ctx} backend={BACKEND_URL} />
      <SimilarTickets ctx={ctx} backend={BACKEND_URL} />
      <div className="section sidebar-actions">
        <button className="esip-btn esip-btn--modal" onClick={openModal}>
          Details &amp; Chat ↗
        </button>
        <button
          className="esip-btn esip-btn--secondary"
          onClick={() => {
            void trackZafImpactEvent(BACKEND_URL, ctx.sessionToken, {
              event_type: 'ticket_link_clicked',
              surface: 'zaf_sidebar',
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
      </div>
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppInner />
    </ErrorBoundary>
  )
}
