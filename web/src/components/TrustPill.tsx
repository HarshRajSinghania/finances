import { CircleAlert, CircleCheck, CircleHelp } from "lucide-react"
import { Link } from "react-router-dom"

import { Badge } from "@/components/ui/badge"
import { Hint } from "@/components/ui/misc"
import type { Coverage } from "@/lib/types"

/**
 * The reconciliation verdict, small enough to sit in the header.
 *
 * This is the one thing that should be visible from every screen: it is the
 * project's whole claim, and a number the user cannot trace back to a statement
 * should never be presented as if it were certain. Three states, never two —
 * "nothing was checked" is not the same as "the numbers disagree".
 */
export function TrustPill({ coverage }: { coverage: Coverage }) {
  const { statements, reconciled, mismatched, unchecked, pct, gaps, trusted } = coverage

  if (statements === 0) {
    return (
      <Badge variant="neutral">
        <CircleHelp aria-hidden />
        No statements
      </Badge>
    )
  }

  const variant = trusted ? "good" : mismatched > 0 ? "critical" : "warning"
  const Icon = trusted ? CircleCheck : mismatched > 0 ? CircleAlert : CircleHelp

  const label = trusted
    ? `${reconciled}/${statements} verified`
    : mismatched > 0
      ? `${mismatched} do not add up`
      : `${unchecked} unchecked`

  const detail = trusted ? (
    <>Every statement agrees with its own printed totals, and no months are missing.</>
  ) : (
    <div className="space-y-1">
      <p>
        {reconciled} of {statements} statements reconcile ({pct}%).
        {mismatched > 0 && ` ${mismatched} disagree with their own printed totals.`}
        {unchecked > 0 && ` ${unchecked} printed no total to check against.`}
      </p>
      {gaps.length > 0 && (
        <p>
          {gaps.length} gap{gaps.length === 1 ? "" : "s"} in the statement history — those
          months are missing entirely, so totals are computed on incomplete data.
        </p>
      )}
      <p className="text-[var(--color-ink-muted)]">Open Statements for the detail.</p>
    </div>
  )

  return (
    <Hint label={detail}>
      <Link to="/statements" aria-label="Reconciliation status">
        <Badge variant={variant} className="cursor-pointer">
          <Icon aria-hidden />
          {label}
        </Badge>
      </Link>
    </Hint>
  )
}
