import { Outlet } from 'react-router-dom'
import { ShieldAlert, X } from 'lucide-react'
import { SidebarProvider, SidebarInset } from '@/components/ui/sidebar'
import { AppSidebar } from './AppSidebar'
import { WhatsNewCenter } from './WhatsNewCenter'
import { useAuth } from '@/contexts/AuthContext'

function roleLabel(role: string): string {
  if (role === 'super_admin') return 'Super Admin'
  if (role === 'admin') return 'Admin'
  return 'User'
}

export function AppShell() {
  const { rolePreview, effectiveRole, setPreviewRole } = useAuth()

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset className="h-svh overflow-hidden bg-background">
        <main className="flex-1 overflow-hidden flex flex-col">
          {rolePreview && (
            <div className="flex min-h-10 shrink-0 items-center gap-2 border-b border-amber-300 bg-amber-50 px-4 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              <ShieldAlert className="size-4 shrink-0" />
              <span className="min-w-0 flex-1 truncate">
                Previewing as {roleLabel(effectiveRole)}. Write actions are blocked.
              </span>
              <button
                type="button"
                onClick={() => { void setPreviewRole(null) }}
                className="inline-flex size-7 shrink-0 items-center justify-center rounded-md hover:bg-amber-100 dark:hover:bg-amber-900/60"
                aria-label="Exit role preview"
                title="Exit role preview"
              >
                <X className="size-4" />
              </button>
            </div>
          )}
          <Outlet />
        </main>
      </SidebarInset>
      <WhatsNewCenter />
    </SidebarProvider>
  )
}
