import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  type ReactNode,
} from 'react'
import { toast } from 'sonner'
import { apiFetch } from '@/lib/api'
import { useAuth } from '@/contexts/AuthContext'
import type { UserProfile, TabInfo, SidebarSections } from '@/types/api'

// ---------------------------------------------------------------------------
// Context shape
// ---------------------------------------------------------------------------

interface ProfileContextValue {
  profile: UserProfile | null
  addTab: (ticket: TabInfo) => void
  removeTab: (ticketId: number) => void
  toggleTheme: () => void
  updateSidebarSections: (sections: SidebarSections) => void
  addSearchHistory: (query: string, type: 'ticket' | 'store') => void
}

const ProfileContext = createContext<ProfileContextValue | null>(null)

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function ProfileProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const [profile, setProfile] = useState<UserProfile | null>(null)

  // Apply dark/light class to <html> whenever theme changes
  useEffect(() => {
    const root = document.documentElement
    if (profile?.theme === 'dark') {
      root.classList.add('dark')
    } else {
      root.classList.remove('dark')
    }
  }, [profile?.theme])

  // Fetch profile from backend on login
  useEffect(() => {
    if (!user) {
      // Authentication is an external source; clear user-scoped data on logout.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setProfile(null)
      return
    }
    apiFetch<UserProfile>('/api/profile')
      .then(setProfile)
      .catch(() => {
        // Non-fatal: app still works without profile (tabs/theme won't persist)
      })
  }, [user])

  // ---------------------------------------------------------------------------
  // Tab management
  // ---------------------------------------------------------------------------

  const addTab = useCallback((ticket: TabInfo) => {
    apiFetch<{ tabs: TabInfo[]; evicted_id: number | null }>('/api/profile/tabs', {
      method: 'POST',
      body: JSON.stringify({
        ticket_id: ticket.id,
        subject: ticket.subject,
        status: ticket.status,
        priority: ticket.priority,
      }),
    })
      .then(({ tabs, evicted_id }) => {
        setProfile((prev) => prev ? { ...prev, open_tabs: tabs } : prev)
        if (evicted_id != null) {
          toast(`Ticket #${evicted_id} removed from open sessions`, {
            description: 'Maximum 8 open tickets reached.',
            duration: 4000,
          })
        }
      })
      .catch(() => {
        // Silently ignore tab persistence failures — ticket still opens fine
      })
  }, [])

  const removeTab = useCallback((ticketId: number) => {
    // Optimistic update — remove immediately in UI
    setProfile((prev) =>
      prev ? { ...prev, open_tabs: prev.open_tabs.filter((t) => t.id !== ticketId) } : prev
    )
    apiFetch(`/api/profile/tabs/${ticketId}`, { method: 'DELETE' }).catch(() => {})
  }, [])

  // ---------------------------------------------------------------------------
  // Theme
  // ---------------------------------------------------------------------------

  const toggleTheme = useCallback(() => {
    setProfile((prev) => {
      if (!prev) return prev
      const next: 'light' | 'dark' = prev.theme === 'light' ? 'dark' : 'light'
      apiFetch('/api/profile', {
        method: 'PATCH',
        body: JSON.stringify({ theme: next }),
      }).catch(() => {})
      return { ...prev, theme: next }
    })
  }, [])

  // ---------------------------------------------------------------------------
  // Sidebar section layout
  // ---------------------------------------------------------------------------

  const updateSidebarSections = useCallback((sections: SidebarSections) => {
    setProfile((prev) => prev ? { ...prev, sidebar_sections: sections } : prev)
    apiFetch('/api/profile', {
      method: 'PATCH',
      body: JSON.stringify({ sidebar_sections: sections }),
    }).catch(() => {})
  }, [])

  // ---------------------------------------------------------------------------
  // Search history
  // ---------------------------------------------------------------------------

  const addSearchHistory = useCallback((query: string, type: 'ticket' | 'store') => {
    // Optimistic update
    setProfile((prev) => {
      if (!prev) return prev
      const history = prev.search_history.filter(
        (h) => !(h.query === query && h.type === type)
      )
      history.unshift({ query, type })
      return { ...prev, search_history: history.slice(0, 10) }
    })
    apiFetch('/api/profile/search-history', {
      method: 'POST',
      body: JSON.stringify({ query, type }),
    }).catch(() => {})
  }, [])

  return (
    <ProfileContext.Provider value={{ profile, addTab, removeTab, toggleTheme, updateSidebarSections, addSearchHistory }}>
      {children}
    </ProfileContext.Provider>
  )
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useProfile() {
  const ctx = useContext(ProfileContext)
  if (!ctx) throw new Error('useProfile must be used inside ProfileProvider')
  return ctx
}
