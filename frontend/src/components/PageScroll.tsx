import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

interface PageScrollProps {
  children: ReactNode
  className?: string
}

export function PageScroll({ children, className }: PageScrollProps) {
  return (
    <div className={cn('h-full min-h-0 overflow-y-auto bg-background', className)}>
      {children}
    </div>
  )
}
