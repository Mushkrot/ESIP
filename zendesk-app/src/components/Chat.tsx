import type { AppContext } from '../App'
import { platformTicketUrl } from '../config'

interface Props {
  ctx: AppContext
}

export default function Chat({ ctx }: Props) {
  function openPlatform() {
    window.open(platformTicketUrl(ctx.ticketId), '_blank', 'noopener,noreferrer')
  }

  return (
    <div className="section">
      <div className="detail-label" style={{ marginBottom: 8 }}>
        Ticket #{ctx.ticketId}
      </div>
      <button className="esip-btn" onClick={openPlatform}>
        Open in ESIP →
      </button>
      <div className="esip-hint">
        Full chat, detailed summary, analytics
      </div>
    </div>
  )
}
