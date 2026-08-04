import { LogOut, ChevronRight, X, Sun, Moon, MessageSquarePlus, Database, BookOpen, Map as MapIcon, LayoutDashboard, Bot, MessageSquareMore, LifeBuoy, Network, Bell, ShieldCheck, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'
import { useProfile } from '@/contexts/ProfileContext'
import { FeedbackModal } from '@/components/FeedbackModal'
import { apiFetch } from '@/lib/api'
import { toast } from 'sonner'
import type {
  FeedbackInboxMessage,
  MyUnsolvedTicketsResponse,
  SidebarSectionKey,
  SidebarSections,
} from '@/types/api'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
  useSidebar,
} from '@/components/ui/sidebar'
import {
  Avatar,
  AvatarFallback,
  AvatarImage,
} from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

// Color dot per ticket status
function StatusDot({ status }: { status: string }) {
  const normalized = status.toLowerCase()
  const color =
    normalized === 'open' || normalized === 'new' ? 'bg-destructive' :
    normalized === 'pending' || normalized === 'hold' ? 'bg-yellow-500' :
    normalized === 'solved' || normalized === 'closed' ? 'bg-green-500' :
    'bg-muted-foreground'
  return <span className={`size-2 rounded-full shrink-0 ${color}`} />
}

function formatShortAge(value: string | null | undefined) {
  if (!value) return ''
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return ''
  const diffMs = Date.now() - timestamp
  const absMinutes = Math.max(0, Math.round(diffMs / 60000))
  if (absMinutes < 1) return 'just now'
  if (absMinutes < 60) return `${absMinutes}m ago`
  const hours = Math.round(absMinutes / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function myUnsolvedTicketTooltip(ticket: {
  id: number
  subject: string
  status: string
  store_id?: string | null
  org_name?: string | null
  updated_at?: string | null
}) {
  const details = [
    ticket.status,
    ticket.store_id ? `Store ${ticket.store_id}` : '',
    ticket.org_name,
    ticket.updated_at ? `updated ${formatShortAge(ticket.updated_at)}` : '',
  ].filter(Boolean)
  return `#${ticket.id} — ${ticket.subject}${details.length > 0 ? ` (${details.join(' · ')})` : ''}`
}

function isUnsolvedTicketStatus(status: string | null | undefined) {
  const normalized = (status ?? '').toLowerCase()
  return normalized === 'new' || normalized === 'open' || normalized === 'pending' || normalized === 'hold'
}

function shouldRefreshMyUnsolvedOnFocus(fetchedAt: string | null | undefined) {
  if (!fetchedAt) return true
  const timestamp = new Date(fetchedAt).getTime()
  if (!Number.isFinite(timestamp)) return true
  return Date.now() - timestamp > 60000
}

type SidebarSectionState = Record<SidebarSectionKey, boolean>

const DEFAULT_SIDEBAR_SECTIONS: SidebarSectionState = {
  tools: true,
  admin: true,
  my_unsolved: true,
  open_in_esip: false,
}

function mergeSidebarSections(sections: SidebarSections | undefined): SidebarSectionState {
  return { ...DEFAULT_SIDEBAR_SECTIONS, ...(sections ?? {}) }
}

function CollapsibleSectionHeader({
  title,
  expanded,
  onToggle,
  rightAction,
}: {
  title: string
  expanded: boolean
  onToggle: () => void
  rightAction?: ReactNode
}) {
  return (
    <div className="flex h-8 items-center justify-between gap-1 px-2 group-data-[collapsible=icon]:hidden">
      <button
        type="button"
        className="-ml-1 flex min-w-0 flex-1 items-center gap-1 rounded-md px-1 py-1 text-left text-xs font-medium text-sidebar-foreground/70 outline-hidden hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <ChevronRight className={`size-3.5 shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`} />
        <span className="truncate">{title}</span>
      </button>
      {rightAction}
    </div>
  )
}

export function AppSidebar() {
  const {
    user,
    logout,
    isAdmin,
    role,
    actualRole,
    previewRole,
    canPreviewRoles,
    setPreviewRole,
  } = useAuth()
  const isSuperAdmin = role === 'super_admin'
  const { profile, removeTab, toggleTheme, updateSidebarSections } = useProfile()
  const { state } = useSidebar()
  const navigate = useNavigate()
  const location = useLocation()
  const { ticketId } = useParams<{ ticketId?: string }>()
  const chatTicketParam = location.pathname === '/chat'
    ? new URLSearchParams(location.search).get('ticket')
    : null
  const activeId = ticketId
    ? parseInt(ticketId, 10)
    : chatTicketParam ? parseInt(chatTicketParam, 10) : null
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackInbox, setFeedbackInbox] = useState<FeedbackInboxMessage[]>([])
  const [myUnsolved, setMyUnsolved] = useState<MyUnsolvedTicketsResponse | null>(null)
  const [myUnsolvedLoading, setMyUnsolvedLoading] = useState(false)
  const [myUnsolvedRefreshing, setMyUnsolvedRefreshing] = useState(false)
  const [myUnsolvedWarning, setMyUnsolvedWarning] = useState('')
  const toastedFeedbackIds = useRef<Set<string>>(new Set())

  const loadFeedbackInbox = useCallback(async () => {
    if (!user) return
    const items = await apiFetch<FeedbackInboxMessage[]>('/api/feedback/inbox')
    setFeedbackInbox(items)

    const newItems = items.filter(item => !toastedFeedbackIds.current.has(item.id))
    if (newItems.length > 0) {
      const top = newItems[0]
      toast(`You have ${items.length} new feedback repl${items.length === 1 ? 'y' : 'ies'}`, {
        description: top.preview,
        duration: 9000,
        action: {
          label: 'Open',
          onClick: () => setFeedbackOpen(true),
        },
      })
    }
    items.forEach(item => toastedFeedbackIds.current.add(item.id))
  }, [user])

  useEffect(() => {
    toastedFeedbackIds.current.clear()
    setFeedbackInbox([])
    if (!user) return

    void loadFeedbackInbox().catch(() => {})
    const interval = window.setInterval(() => {
      void loadFeedbackInbox().catch(() => {})
    }, 45000)
    const onFocus = () => {
      void loadFeedbackInbox().catch(() => {})
    }
    window.addEventListener('focus', onFocus)

    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', onFocus)
    }
  }, [user, loadFeedbackInbox])

  const loadMyUnsolvedTickets = useCallback(async (mode: 'load' | 'refresh' | 'silent' = 'load') => {
    if (!user) return
    if (mode === 'refresh') setMyUnsolvedRefreshing(true)
    else if (mode === 'load') setMyUnsolvedLoading(true)
    setMyUnsolvedWarning('')

    try {
      const endpoint = mode === 'refresh'
        ? '/api/profile/my-unsolved-tickets/refresh?limit=20'
        : '/api/profile/my-unsolved-tickets?limit=20'
      const data = await apiFetch<MyUnsolvedTicketsResponse>(
        endpoint,
        mode === 'refresh' ? { method: 'POST' } : undefined
      )
      setMyUnsolved(data)
      if (data.warning) setMyUnsolvedWarning(data.warning)
    } catch (err) {
      setMyUnsolvedWarning(err instanceof Error ? err.message : 'Could not load assigned tickets.')
    } finally {
      if (mode === 'refresh') setMyUnsolvedRefreshing(false)
      else if (mode === 'load') setMyUnsolvedLoading(false)
    }
  }, [user])

  useEffect(() => {
    setMyUnsolved(null)
    setMyUnsolvedWarning('')
    if (!user) return
    void loadMyUnsolvedTickets('load')
  }, [user, loadMyUnsolvedTickets])

  useEffect(() => {
    if (!user || !myUnsolved) return
    if (myUnsolved.refresh_status !== 'queued' && myUnsolved.refresh_status !== 'refreshing') return
    const timeout = window.setTimeout(() => {
      void loadMyUnsolvedTickets('silent')
    }, 5000)
    return () => window.clearTimeout(timeout)
  }, [user, myUnsolved, loadMyUnsolvedTickets])

  useEffect(() => {
    if (!user) return
    const onFocus = () => {
      if (myUnsolvedRefreshing) return
      if (!shouldRefreshMyUnsolvedOnFocus(myUnsolved?.fetched_at)) return
      void loadMyUnsolvedTickets('refresh')
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [user, myUnsolved?.fetched_at, myUnsolvedRefreshing, loadMyUnsolvedTickets])

  const initials = user?.displayName
    ?.split(' ')
    .map((n) => n[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() ?? '?'

  const tabs = profile?.open_tabs ?? []
  const latestOpenTabStatusById = new Map(tabs.map(tab => [tab.id, tab.status]))
  const myUnsolvedItems = (myUnsolved?.items ?? []).filter((ticket) => {
    const latestStatus = latestOpenTabStatusById.get(ticket.id)
    return latestStatus ? isUnsolvedTicketStatus(latestStatus) : isUnsolvedTicketStatus(ticket.status)
  })
  const sidebarSections = mergeSidebarSections(profile?.sidebar_sections)
  const showTools = sidebarSections.tools || state === 'collapsed'
  const showAdmin = sidebarSections.admin || state === 'collapsed'
  const isDark = profile?.theme === 'dark'
  const triggerLabel = state === 'collapsed' ? 'Open navigation' : 'Close navigation'
  const roleLabel = role === 'super_admin' ? 'Super Admin' : role === 'admin' ? 'Admin' : 'User'
  const actualRoleLabel = actualRole === 'super_admin' ? 'Super Admin' : actualRole === 'admin' ? 'Admin' : 'User'
  const toggleSidebarSection = (section: SidebarSectionKey) => {
    updateSidebarSections({
      ...sidebarSections,
      [section]: !sidebarSections[section],
    })
  }

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-3 py-3 group-data-[collapsible=icon]:px-2 group-data-[collapsible=icon]:py-2">
        <SidebarMenu>
          <SidebarMenuItem>
            <div className="flex min-w-0 items-center gap-3 group-data-[collapsible=icon]:justify-center">
              <img
                src="/esip-mark.svg"
                alt="ESIP"
                className="size-11 shrink-0 rounded-[10px] object-contain shadow-sm ring-1 ring-white/10 group-data-[collapsible=icon]:hidden"
              />
              <div className="grid min-w-0 flex-1 text-left leading-tight group-data-[collapsible=icon]:hidden">
                <span className="truncate text-base font-semibold tracking-[0.02em] text-sidebar-foreground">ESIP</span>
                <span className="text-xs leading-tight text-sidebar-foreground/60">
                  <span className="block">Enterprise Support</span>
                  <span className="block">Intelligence Platform</span>
                </span>
              </div>
              <SidebarTrigger
                title={triggerLabel}
                aria-label={triggerLabel}
                className="ml-auto size-8 shrink-0 group-data-[collapsible=icon]:ml-0"
              />
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        {/* ── Tools ─────────────────────────────────────── */}
        <SidebarGroup>
          <CollapsibleSectionHeader
            title="Tools"
            expanded={sidebarSections.tools}
            onToggle={() => toggleSidebarSection('tools')}
          />
          {showTools && (
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  tooltip="AI Chat — Universal mode"
                  isActive={location.pathname === '/chat'}
                  onClick={() => {
                    const saved = sessionStorage.getItem('lastChatSearch')
                    navigate(saved ? `/chat?${saved}` : '/chat')
                  }}
                >
                  <Bot />
                  <span>AI Chat</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton asChild tooltip="Knowledge gap analysis">
                  <Link to="/knowledge-gap">
                    <BookOpen />
                    <span>Knowledge Gaps</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton asChild tooltip="Quick user guide">
                  <Link to="/guide">
                    <LifeBuoy />
                    <span>Quick Guide</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  tooltip="What’s New"
                  onClick={() => window.dispatchEvent(new Event('esip:open-whats-new'))}
                >
                  <Bell />
                  <span>What’s New</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton asChild tooltip="Road Map">
                  <Link to="/roadmap" target="_blank" rel="noopener noreferrer">
                    <MapIcon />
                    <span>Road Map</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          )}
        </SidebarGroup>

        <SidebarSeparator />

        {/* ── Admin ─────────────────────────────────────── */}
        {isAdmin && (
          <>
            <SidebarGroup>
              <CollapsibleSectionHeader
                title="Admin"
                expanded={sidebarSections.admin}
                onToggle={() => toggleSidebarSection('admin')}
              />
              {showAdmin && (
                <SidebarMenu>
                  <SidebarMenuItem>
                    <SidebarMenuButton asChild tooltip="Admin Dashboard">
                      <Link to="/admin/panel">
                        <LayoutDashboard />
                        <span>Admin Dashboard</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                  <SidebarMenuItem>
                    <SidebarMenuButton asChild tooltip="Data sources">
                      <Link to="/admin/sources">
                        <Database />
                        <span>Sources</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                  {isSuperAdmin && (
                    <SidebarMenuItem>
                      <SidebarMenuButton asChild tooltip="Beta feedback (Super Admin)">
                        <Link to="/admin/feedback">
                          <MessageSquareMore />
                          <span>Feedbacks</span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  )}
                  <SidebarMenuItem>
                    <SidebarMenuButton asChild tooltip="Architecture diagrams">
                      <Link to="/admin/architecture">
                        <Network />
                        <span>Architecture</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                </SidebarMenu>
              )}
            </SidebarGroup>
            <SidebarSeparator />
          </>
        )}

        {/* My Unsolved Tickets */}
        <SidebarGroup className="group-data-[collapsible=icon]:hidden">
          <CollapsibleSectionHeader
            title="My Unsolved Tickets"
            expanded={sidebarSections.my_unsolved}
            onToggle={() => toggleSidebarSection('my_unsolved')}
            rightAction={(
              <button
                type="button"
                className="inline-flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground disabled:opacity-50"
                onClick={() => void loadMyUnsolvedTickets('refresh')}
                disabled={myUnsolvedRefreshing}
                title="Refresh from Zendesk"
                aria-label="Refresh my unsolved tickets from Zendesk"
              >
                <RefreshCw className={`size-3.5 ${myUnsolvedRefreshing ? 'animate-spin' : ''}`} />
              </button>
            )}
          />
          {sidebarSections.my_unsolved && (
            <>
              <div className="max-h-80 overflow-y-auto pr-1">
                <SidebarMenu>
                  {myUnsolvedLoading && (
                    <p className="px-2 py-1 text-xs text-muted-foreground">
                      Loading assigned tickets...
                    </p>
                  )}
                  {!myUnsolvedLoading && myUnsolvedItems.length === 0 && (
                    <p className="px-2 py-1 text-xs text-muted-foreground">
                      No unsolved tickets assigned to you.
                    </p>
                  )}
                  {myUnsolvedItems.map((ticket) => {
                    const ticketTitle = myUnsolvedTicketTooltip(ticket)
                    return (
                      <SidebarMenuItem key={ticket.id}>
                        <SidebarMenuButton
                          isActive={ticket.id === activeId}
                          onClick={() => navigate(`/chat?ticket=${ticket.id}`)}
                          title={ticketTitle}
                          tooltip={ticketTitle}
                        >
                          <StatusDot status={ticket.status} />
                          <div className="flex min-w-0 flex-col">
                            <span className="font-mono text-xs text-muted-foreground leading-tight">#{ticket.id}</span>
                            <span className="truncate text-xs leading-tight">{ticket.subject}</span>
                          </div>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    )
                  })}
                </SidebarMenu>
              </div>
              <div className="px-2 pt-1 text-[10px] leading-tight text-muted-foreground/75">
                {myUnsolvedRefreshing
                  ? 'Refreshing from Zendesk...'
                  : myUnsolved?.refresh_status === 'queued'
                    ? 'Updating from Zendesk in background...'
                    : myUnsolved?.fetched_at
                      ? `${myUnsolved.count} assigned · updated ${formatShortAge(myUnsolved.fetched_at)}`
                      : null}
                {myUnsolvedWarning && (
                  <div className="mt-1 text-yellow-600 dark:text-yellow-400">{myUnsolvedWarning}</div>
                )}
              </div>
            </>
          )}
        </SidebarGroup>

        <SidebarSeparator className="group-data-[collapsible=icon]:hidden" />

        {/* Open in ESIP */}
        <SidebarGroup className="group-data-[collapsible=icon]:hidden">
          <CollapsibleSectionHeader
            title="Open in ESIP"
            expanded={sidebarSections.open_in_esip}
            onToggle={() => toggleSidebarSection('open_in_esip')}
          />
          {sidebarSections.open_in_esip && (
            <div className="max-h-72 overflow-y-auto pr-1">
              <SidebarMenu>
                {tabs.length === 0 && (
                  <p className="px-2 py-1 text-xs text-muted-foreground">No open tickets</p>
                )}
                {tabs.map((tab) => (
                  <SidebarMenuItem key={tab.id}>
                    <SidebarMenuButton
                      isActive={tab.id === activeId}
                      onClick={() => navigate(`/chat?ticket=${tab.id}`)}
                      tooltip={`#${tab.id} — ${tab.subject}`}
                      className="pr-7"
                    >
                      <StatusDot status={tab.status} />
                      <div className="flex flex-col min-w-0">
                        <span className="font-mono text-xs text-muted-foreground leading-tight">#{tab.id}</span>
                        <span className="truncate text-xs leading-tight">{tab.subject}</span>
                      </div>
                    </SidebarMenuButton>
                    <SidebarMenuAction
                      onClick={(e) => {
                        e.stopPropagation()
                        removeTab(tab.id)
                        if (tab.id === activeId) navigate('/chat')
                      }}
                      showOnHover
                    >
                      <X className="size-3.5" />
                      <span className="sr-only">Close ticket #{tab.id}</span>
                    </SidebarMenuAction>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </div>
          )}
        </SidebarGroup>
      </SidebarContent>

      <FeedbackModal
        open={feedbackOpen}
        onClose={() => setFeedbackOpen(false)}
        inbox={feedbackInbox}
        onInboxChange={setFeedbackInbox}
      />

      <SidebarFooter>
        <SidebarMenu>
          {/* Leave Feedback button — highlighted for beta testers */}
          <SidebarMenuItem>
            <SidebarMenuButton
              onClick={() => setFeedbackOpen(true)}
              tooltip="Leave feedback for the development team"
              className="relative bg-amber-50 text-amber-700 border border-amber-200
                         hover:bg-amber-100 hover:text-amber-800
                         dark:bg-amber-950/40 dark:text-amber-400 dark:border-amber-800
                         dark:hover:bg-amber-950/60 font-medium"
            >
              <MessageSquarePlus className="shrink-0" />
              <span>Leave Feedback</span>
              {feedbackInbox.length > 0 && (
                <span
                  className="ml-auto flex min-w-5 items-center justify-center rounded-full bg-destructive px-1.5 py-0.5 text-[10px] font-semibold leading-none text-destructive-foreground group-data-[collapsible=icon]:absolute group-data-[collapsible=icon]:right-1 group-data-[collapsible=icon]:top-1 group-data-[collapsible=icon]:ml-0 group-data-[collapsible=icon]:size-2 group-data-[collapsible=icon]:min-w-0 group-data-[collapsible=icon]:p-0"
                  aria-label={`${feedbackInbox.length} unread feedback replies`}
                >
                  <span className="group-data-[collapsible=icon]:sr-only">
                    {feedbackInbox.length > 9 ? '9+' : feedbackInbox.length}
                  </span>
                </span>
              )}
            </SidebarMenuButton>
          </SidebarMenuItem>

          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton
                  size="lg"
                  className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
                >
                  <Avatar className="size-8 rounded-lg">
                    <AvatarImage src={user?.photoURL ?? ''} alt={user?.displayName ?? ''} />
                    <AvatarFallback className="rounded-lg">{initials}</AvatarFallback>
                  </Avatar>
                  <div className="grid flex-1 text-left text-sm leading-tight">
                    <span className="truncate font-semibold">{user?.displayName}</span>
                    <span className="truncate text-xs text-muted-foreground">{user?.email}</span>
                  </div>
                  <ChevronRight className="ml-auto size-4" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                className="w-64 rounded-lg"
                side="right"
                align="end"
                sideOffset={4}
              >
                {canPreviewRoles && (
                  <>
                    <DropdownMenuLabel className="px-2 py-1 text-xs text-muted-foreground">
                      Role preview
                    </DropdownMenuLabel>
                    <DropdownMenuRadioGroup
                      value={previewRole ?? 'actual'}
                      onValueChange={(value) => {
                        void setPreviewRole(value === 'actual' ? null : value as 'admin' | 'user')
                      }}
                    >
                      <DropdownMenuRadioItem value="actual">
                        <ShieldCheck className="mr-2 size-4" />
                        Actual: {actualRoleLabel}
                      </DropdownMenuRadioItem>
                      <DropdownMenuRadioItem value="admin">
                        Preview: Admin
                      </DropdownMenuRadioItem>
                      <DropdownMenuRadioItem value="user">
                        Preview: User
                      </DropdownMenuRadioItem>
                    </DropdownMenuRadioGroup>
                    <div className="px-2 pb-1 pt-0.5 text-xs text-muted-foreground">
                      Active role: {roleLabel}
                    </div>
                    <DropdownMenuSeparator />
                  </>
                )}
                <DropdownMenuItem onClick={toggleTheme}>
                  {isDark
                    ? <Sun className="mr-2 size-4" />
                    : <Moon className="mr-2 size-4" />
                  }
                  {isDark ? 'Light mode' : 'Dark mode'}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() => {
                    void logout().then(() => navigate('/login'))
                  }}
                >
                  <LogOut className="mr-2 size-4" />
                  Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
