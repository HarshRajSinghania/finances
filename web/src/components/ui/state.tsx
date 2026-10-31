import { AlertTriangle, Inbox, Loader2 } from "lucide-react"
import type * as React from "react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/misc"
import { cn } from "@/lib/utils"

/**
 * The three states every data view has to handle.
 *
 * They live together because the failure mode this prevents is a page that
 * renders "$0" while it is still loading, or "no transactions" when the API is
 * simply not running. An empty result and an unreachable server are different
 * things and must never look the same.
 */

export function EmptyState({
  icon: Icon = Inbox,
  title,
  body,
  action,
  className,
}: {
  icon?: React.ElementType
  title: string
  body?: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 px-6 py-14 text-center",
        className,
      )}
    >
      <Icon className="size-7 text-[var(--color-ink-muted)]" strokeWidth={1.5} />
      <div className="space-y-1">
        <p className="text-sm font-medium">{title}</p>
        {body && (
          <p className="mx-auto max-w-sm text-xs leading-relaxed text-[var(--color-ink-secondary)]">
            {body}
          </p>
        )}
      </div>
      {action}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: Error
  onRetry?: () => void
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 px-6 py-14 text-center",
        className,
      )}
    >
      <AlertTriangle className="size-7 text-[var(--color-critical)]" strokeWidth={1.5} />
      <div className="space-y-1">
        <p className="text-sm font-medium">Could not load this</p>
        <p className="mx-auto max-w-md font-mono text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          {error.message}
        </p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

export function LoadingRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2 p-5", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-4 animate-spin", className)} />
}
