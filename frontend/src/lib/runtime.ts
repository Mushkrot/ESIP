import { REFERENCE_IDENTITIES } from '@/reference_data/fixtures'

export const RUNTIME_PROFILE = import.meta.env.VITE_RUNTIME_PROFILE === 'connected'
  ? 'connected'
  : 'reference'

export const IS_REFERENCE_RUNTIME = RUNTIME_PROFILE === 'reference'
export const REFERENCE_SESSION_TOKEN = 'esip-reference-session'

export const REFERENCE_SESSION_USER = {
  uid: 'reference-platform-admin',
  email: REFERENCE_IDENTITIES.platformAdmin,
  displayName: 'Morgan Chen',
  photoURL: null,
} as const
