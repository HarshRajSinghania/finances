import { cva, type VariantProps } from "class-variance-authority"
import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * Status variants map to the reserved status palette, never to a series colour.
 * They always ship with a label (and usually an icon) beside them, because on
 * the light surface `warning` and `serious` sit below 3:1 — colour alone is
 * never allowed to carry the meaning.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3",
  {
    variants: {
      variant: {
        neutral:
          "border-[var(--color-hairline)] bg-[var(--color-plane)] text-[var(--color-ink-secondary)]",
        good: "border-[var(--color-good)]/30 bg-[var(--color-good)]/10 text-[var(--color-success-text)]",
        warning:
          "border-[var(--color-warning)]/40 bg-[var(--color-warning)]/15 text-[var(--color-ink)]",
        serious:
          "border-[var(--color-serious)]/40 bg-[var(--color-serious)]/15 text-[var(--color-ink)]",
        critical:
          "border-[var(--color-critical)]/35 bg-[var(--color-critical)]/10 text-[var(--color-critical)]",
        outline: "border-[var(--color-hairline)] text-[var(--color-ink-secondary)]",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { badgeVariants }
