import { authHeaders } from './api'

export type ImpactEvent = {
  event_type: string
  surface?: 'web' | 'zaf_sidebar' | 'zaf_modal' | 'universal_chat'
  workflow?: string
  ticket_id?: number | null
  target_ticket_id?: number | null
  conversation_id?: string | null
  turn_id?: string | null
  trace_id?: string | null
  object_type?: string | null
  object_id?: string | null
  object_label?: string | null
  target_url?: string | null
  metadata?: Record<string, unknown>
}

export async function trackImpactEvent(event: ImpactEvent): Promise<void> {
  try {
    const headers = await authHeaders({ 'Content-Type': 'application/json' })
    await fetch('/api/telemetry/impact-event', {
      method: 'POST',
      headers,
      body: JSON.stringify({ surface: 'web', ...event }),
      keepalive: true,
    })
  } catch {
    // Product telemetry is best-effort and must never interrupt user work.
  }
}
