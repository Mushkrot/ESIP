import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { apiFetch, getStoredRolePreview, setStoredRolePreview, type RolePreview } from '@/lib/api'
import { IS_REFERENCE_RUNTIME, REFERENCE_SESSION_USER } from '@/lib/runtime'

export type UserRole = 'user' | 'admin' | 'super_admin'

interface AuthContextValue {
  user: SessionUser | null
  loading: boolean
  isAdmin: boolean       // true for admin + super_admin (backward compat)
  role: UserRole
  actualRole: UserRole
  effectiveRole: UserRole
  rolePreview: boolean
  previewRole: RolePreview | null
  canPreviewRoles: boolean
  setPreviewRole: (role: RolePreview | null) => Promise<void>
  signIn: () => Promise<void>
  logout: () => Promise<void>
}

interface SessionUser {
  uid: string
  email: string | null
  displayName: string | null
  photoURL: string | null
}

const AuthContext = createContext<AuthContextValue | null>(null)

type MeResponse = {
  uid: string
  email: string
  name: string
  picture: string | null
  is_admin: boolean
  role: UserRole
  actual_role?: UserRole
  effective_role?: UserRole
  role_preview?: boolean
  can_preview_roles?: boolean
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(
    IS_REFERENCE_RUNTIME ? REFERENCE_SESSION_USER : null,
  )
  const [loading, setLoading] = useState(true)
  const [isAdmin, setIsAdmin] = useState(false)
  const [role, setRole] = useState<UserRole>('user')
  const [actualRole, setActualRole] = useState<UserRole>('user')
  const [effectiveRole, setEffectiveRole] = useState<UserRole>('user')
  const [rolePreview, setRolePreview] = useState(false)
  const [previewRole, setPreviewRoleState] = useState<RolePreview | null>(() => getStoredRolePreview())
  const [canPreviewRoles, setCanPreviewRoles] = useState(false)

  const applyMe = useCallback((me: MeResponse) => {
    const nextRole = me.effective_role ?? me.role ?? 'user'
    setIsAdmin(Boolean(me.is_admin))
    setRole(nextRole)
    setEffectiveRole(nextRole)
    setActualRole(me.actual_role ?? nextRole)
    setRolePreview(Boolean(me.role_preview))
    setCanPreviewRoles(Boolean(me.can_preview_roles))
    setPreviewRoleState(getStoredRolePreview())
  }, [])

  const resetAuthState = useCallback(() => {
    setIsAdmin(false)
    setRole('user')
    setActualRole('user')
    setEffectiveRole('user')
    setRolePreview(false)
    setCanPreviewRoles(false)
  }, [])

  const loadMe = useCallback(async () => {
    try {
      applyMe(await apiFetch<MeResponse>('/api/auth/me'))
    } catch (err) {
      if (getStoredRolePreview()) {
        setStoredRolePreview(null)
        setPreviewRoleState(null)
        applyMe(await apiFetch<MeResponse>('/api/auth/me'))
        return
      }
      resetAuthState()
      throw err
    }
  }, [applyMe, resetAuthState])

  useEffect(() => {
    if (IS_REFERENCE_RUNTIME) {
      void loadMe()
        .catch(() => {})
        .finally(() => setLoading(false))
      return
    }

    let cancelled = false
    let unsubscribe: (() => void) | undefined
    void Promise.all([import('firebase/auth'), import('@/lib/firebase')]).then(([
      { onAuthStateChanged },
      { auth },
    ]) => {
      if (cancelled) return
      unsubscribe = onAuthStateChanged(auth, (nextUser) => {
        setUser(nextUser)
        if (!nextUser) {
          setStoredRolePreview(null)
          setPreviewRoleState(null)
          resetAuthState()
          setLoading(false)
          return
        }
        setLoading(true)
        void loadMe()
          .catch(() => {})
          .finally(() => setLoading(false))
      })
    })
    return () => {
      cancelled = true
      unsubscribe?.()
    }
  }, [loadMe, resetAuthState])

  const signIn = async () => {
    if (IS_REFERENCE_RUNTIME) return
    const [{ signInWithPopup }, { auth, googleProvider }] = await Promise.all([
      import('firebase/auth'),
      import('@/lib/firebase'),
    ])
    await signInWithPopup(auth, googleProvider)
  }
  const logout = async () => {
    setStoredRolePreview(null)
    setPreviewRoleState(null)
    if (IS_REFERENCE_RUNTIME) return
    const [{ signOut }, { auth }] = await Promise.all([
      import('firebase/auth'),
      import('@/lib/firebase'),
    ])
    await signOut(auth)
  }

  const setPreviewRole = async (nextRole: RolePreview | null) => {
    const previousRole = getStoredRolePreview()
    setStoredRolePreview(nextRole)
    setPreviewRoleState(nextRole)
    if (!user) return
    setLoading(true)
    try {
      await loadMe()
      if (previousRole !== nextRole && typeof window !== 'undefined') {
        window.location.reload()
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        isAdmin,
        role,
        actualRole,
        effectiveRole,
        rolePreview,
        previewRole,
        canPreviewRoles,
        setPreviewRole,
        signIn,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
