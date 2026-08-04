import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AuthProvider, useAuth } from '@/contexts/AuthContext'
import { ProfileProvider } from '@/contexts/ProfileContext'
import { AppShell } from '@/components/AppShell'
import { LoginPage } from '@/pages/LoginPage'
import { AnalyticsPage } from '@/pages/AnalyticsPage'
import { AdminFeedbackPage } from '@/pages/AdminFeedbackPage'
import { AdminSourcesPage } from '@/pages/AdminSourcesPage'
import { AdminImpactReportPage, AdminPanelPage } from '@/pages/AdminPanelPage'
import { AdminArchitecturePage } from '@/pages/AdminArchitecturePage'
import { KnowledgeGapPage } from '@/pages/KnowledgeGapPage'
import { RoadmapPage } from '@/pages/RoadmapPage'
import { GuidePage } from '@/pages/GuidePage'
import { AIChatPage } from '@/pages/AIChatPage'
import { Loader2 } from 'lucide-react'

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (!user) {
    const next = `${location.pathname}${location.search}${location.hash}`
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />
  }
  return <>{children}</>
}

function LoginGate() {
  const { user } = useAuth()
  const [searchParams] = useSearchParams()
  const rawNext = searchParams.get('next')
  const next = rawNext && rawNext.startsWith('/') && !rawNext.startsWith('//') ? rawNext : '/'
  return user ? <Navigate to={next} replace /> : <LoginPage />
}

function TicketRedirect() {
  const { ticketId } = useParams<{ ticketId?: string }>()
  return <Navigate to={ticketId ? `/chat?ticket=${ticketId}` : '/chat'} replace />
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={<LoginGate />}
      />
      <Route
        path="/"
        element={
          <AuthGuard>
            <ProfileProvider>
              <AppShell />
            </ProfileProvider>
          </AuthGuard>
        }
      >
        <Route index element={<Navigate to="/chat" replace />} />
        <Route path="ticket/:ticketId" element={<TicketRedirect />} />
        <Route path="chat" element={<AIChatPage />} />
        <Route path="guide" element={<GuidePage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
        <Route path="knowledge-gap" element={<KnowledgeGapPage />} />
        <Route path="roadmap" element={<RoadmapPage />} />
        <Route path="admin/panel" element={<AdminPanelPage />} />
        <Route path="admin/costs" element={<AdminPanelPage initialTab="costs" />} />
        <Route path="admin/system" element={<AdminPanelPage initialTab="system" />} />
        <Route path="admin/notifications" element={<AdminPanelPage initialTab="notifications" />} />
        <Route path="admin/users" element={<AdminPanelPage initialTab="users" />} />
        <Route path="admin/usage" element={<AdminPanelPage initialTab="usage" initialInsightMode="esip" />} />
        <Route path="admin/insights" element={<AdminPanelPage initialTab="usage" initialInsightMode="esip" />} />
        <Route path="admin/insights/esip-usage" element={<AdminPanelPage initialTab="usage" initialInsightMode="esip" />} />
        <Route path="admin/insights/service-desk-kpi" element={<AdminPanelPage initialTab="usage" initialInsightMode="kpi" />} />
        <Route path="admin/insights/service-desk-kpi/email-delivery" element={<AdminPanelPage initialTab="usage" initialInsightMode="kpi-email" />} />
        <Route path="admin/impact" element={<AdminPanelPage initialTab="impact" />} />
        <Route path="admin/changes" element={<AdminPanelPage initialTab="changes" />} />
        <Route path="admin/quality" element={<AdminPanelPage initialTab="quality" />} />
        <Route path="admin/chat-logs" element={<AdminPanelPage initialTab="chat-logs" />} />
        <Route path="admin/impact/report" element={<AdminImpactReportPage />} />
        <Route path="admin/feedback" element={<AdminFeedbackPage />} />
        <Route path="admin/sources" element={<AdminSourcesPage />} />
        <Route path="admin/architecture" element={<AdminArchitecturePage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <TooltipProvider>
          <AppRoutes />
          <Toaster richColors position="bottom-right" />
        </TooltipProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
