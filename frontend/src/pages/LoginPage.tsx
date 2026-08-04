import { useAuth } from '@/contexts/AuthContext'
import { Button } from '@/components/ui/button'
import { REFERENCE_SUPPORT_ORGANIZATION, REFERENCE_TENANT_DOMAIN } from '@/reference_data/fixtures'
import { IS_REFERENCE_RUNTIME } from '@/lib/runtime'

export function LoginPage() {
  const { signIn } = useAuth()

  return (
    <div className="signal-login min-h-screen flex items-center justify-center bg-background px-5 py-10">
      <div className="signal-login-card flex w-full max-w-sm flex-col items-center gap-6 rounded-xl border bg-card p-8 shadow-xl">
        <div className="flex items-center gap-4 self-stretch">
          <img
            src="/esip-mark.svg"
            alt="ESIP"
            className="size-16 shrink-0 rounded-[14px] shadow-sm"
          />
          <div className="min-w-0 text-left">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">Signal Desk</p>
            <p className="text-3xl font-semibold tracking-[-0.035em] text-foreground">ESIP</p>
          </div>
        </div>
        <div className="text-center space-y-1">
          <h1 className="text-xl font-semibold tracking-tight">Enterprise Support Intelligence Platform</h1>
          <p className="text-sm text-muted-foreground">{REFERENCE_SUPPORT_ORGANIZATION}</p>
        </div>

        <Button
          variant="outline"
          className="w-full gap-2"
          onClick={() => void signIn()}
        >
          {IS_REFERENCE_RUNTIME ? null : <GoogleIcon />}
          {IS_REFERENCE_RUNTIME ? 'Open reference workspace' : 'Sign in with Google'}
        </Button>

        <p className="text-xs text-muted-foreground text-center leading-relaxed">
          {IS_REFERENCE_RUNTIME
            ? 'Uses the included Northstar reference environment.'
            : <>Access is restricted to <span className="font-medium">@{REFERENCE_TENANT_DOMAIN}</span> accounts authorized by IT.</>}
        </p>
      </div>
    </div>
  )
}

function GoogleIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
      />
      <path
        fill="#34A853"
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
      />
      <path
        fill="#FBBC05"
        d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
      />
      <path
        fill="#EA4335"
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
      />
    </svg>
  )
}
