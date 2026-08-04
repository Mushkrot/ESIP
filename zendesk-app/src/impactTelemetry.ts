export type ZafImpactEvent = {
  event_type: string
  surface: 'zaf_sidebar' | 'zaf_modal'
  workflow?: string
  ticket_id?: number | null
  target_ticket_id?: number | null
  object_type?: string | null
  object_id?: string | null
  object_label?: string | null
  target_url?: string | null
  metadata?: Record<string, unknown>
}

export async function trackZafImpactEvent(
  backend: string,
  sessionToken: string,
  event: ZafImpactEvent,
): Promise<void> {
  try {
    await fetch(`${backend}/api/telemetry/impact-event`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`,
      },
      body: JSON.stringify(event),
    })
  } catch {
    // Best-effort telemetry only.
  }
}
