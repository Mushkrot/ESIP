const localHost = window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost'

export const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL || '').trim()
  || (localHost ? 'http://127.0.0.1:8000' : 'https://api.esip.example.com')
export const PLATFORM_URL = (import.meta.env.VITE_PLATFORM_URL || '').trim()
  || (localHost ? 'http://127.0.0.1:3000' : 'https://esip.example.com')

export function platformTicketUrl(ticketId: number): string {
  return `${PLATFORM_URL}/chat?ticket=${ticketId}`
}
