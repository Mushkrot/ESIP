import { IS_REFERENCE_RUNTIME, REFERENCE_SESSION_TOKEN } from './runtime'

export type RolePreview = 'admin' | 'user'

const ROLE_PREVIEW_STORAGE_KEY = 'esip.rolePreview'
const ROLE_PREVIEW_HEADER = 'X-ESIP-Preview-Role'

async function getToken(): Promise<string> {
  if (IS_REFERENCE_RUNTIME) return REFERENCE_SESSION_TOKEN
  const { auth } = await import('./firebase')
  const user = auth.currentUser
  if (!user) throw new Error('Not authenticated')
  return user.getIdToken()
}

export function getStoredRolePreview(): RolePreview | null {
  if (typeof window === 'undefined') return null
  const value = window.sessionStorage.getItem(ROLE_PREVIEW_STORAGE_KEY)
  return value === 'admin' || value === 'user' ? value : null
}

export function setStoredRolePreview(role: RolePreview | null): void {
  if (typeof window === 'undefined') return
  if (role) window.sessionStorage.setItem(ROLE_PREVIEW_STORAGE_KEY, role)
  else window.sessionStorage.removeItem(ROLE_PREVIEW_STORAGE_KEY)
}

export function rolePreviewHeaders(): Record<string, string> {
  const role = getStoredRolePreview()
  return role ? { [ROLE_PREVIEW_HEADER]: role } : {}
}

export async function authHeaders(extra?: Record<string, string>): Promise<Record<string, string>> {
  const token = await getToken()
  return {
    Authorization: `Bearer ${token}`,
    ...rolePreviewHeaders(),
    ...extra,
  }
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = await authHeaders({ 'Content-Type': 'application/json' })
  const res = await fetch(path, {
    ...options,
    headers: {
      ...headers,
      ...(options?.headers as Record<string, string> | undefined),
    },
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export { getToken }
