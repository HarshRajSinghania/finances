import type * as React from "react"

import { Hint } from "@/components/ui/misc"
import { Skeleton } from "@/components/ui/misc"
import { cn } from "@/lib/utils"

/**
 * A single headline number.
 *
 * Sometimes the right chart is not a chart. A monthly average is one value with
 * no shape to show, and a bar of length one communicates nothing that the digits
 * do not. The value stays in ink, never in a series colour — colour beside text
 * carries identity, it does not carry the number.
 */
export function Stat({
  label,
  value,
  hint,
  sub,
  loading,
  emphasis = false,
  className,
}: {
  label: string
  value: React.ReactNode
  hint?: React.ReactNode
  sub?: React.ReactNode
  loading?: boolean
  emphasis?: boolean
  className?: string
}) {
  return (
    <div
      className={cn(
        "rounded-[var(--radius-card)] border border-[var(--color-hairline)] bg-[var(--color-surface)] p-4",
        className,
      )}
    >
      <div className="flex items-center gap-1.5">
        <p className="text-xs text-[var(--color-ink-secondary)]">{label}</p>
        {hint && (
          <Hint label={hint}>
            <button
              type="button"
              aria-label={`About ${label}`}
              className="grid size-3.5 place-items-center rounded-full border border-[var(--color-hairline)] text-[9px] leading-none text-[var(--color-ink-muted)]"
            >
              ?
            </button>
          </Hint>
        )}
      </div>

      {loading ? (
        <Skeleton className="mt-2 h-7 w-24" />
      ) : (
        <p
          className={cn(
            "tnum mt-1 font-semibold tracking-tight",
            emphasis ? "text-3xl" : "text-2xl",
          )}
        >
          {value}
        </p>
      )}

      {sub && !loading && (
        <p className="mt-1 text-xs text-[var(--color-ink-muted)]">{sub}</p>
      )}
    </div>
  )
}
