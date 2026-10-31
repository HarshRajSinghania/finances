import { ChartFrame } from "@/components/charts/ChartFrame"
import { money, percent } from "@/lib/format"
import type { CategoryRow } from "@/lib/types"

/**
 * Where the money went, as ranked horizontal bars.
 *
 * One measure, one hue — magnitude is carried by bar length, so giving each
 * category its own colour would add a second encoding that means nothing. Every
 * bar is directly labelled, which is also what satisfies the relief rule for
 * the light surface.
 */
export function CategoryBars({
  categories,
  limit = 10,
  perMonth = false,
}: {
  categories: CategoryRow[]
  limit?: number
  perMonth?: boolean
}) {
  const rows = categories.slice(0, limit)
  const max = Math.max(...rows.map((r) => (perMonth ? r.perMonth : r.total)), 1)
  const hidden = categories.length - rows.length

  return (
    <ChartFrame
      title={perMonth ? "Categories, per month" : "Where the money went"}
      subtitle={
        hidden > 0
          ? `Top ${rows.length} of ${categories.length} categories.`
          : `All ${categories.length} categories.`
      }
      tableHead={["Category", "Total", "Per month", "Share", "Charges"]}
      tableRows={categories.map((c) => [
        c.category,
        money(c.total),
        money(c.perMonth),
        percent(c.share, 1),
        c.count,
      ])}
    >
      <ul className="space-y-2.5">
        {rows.map((c) => {
          const v = perMonth ? c.perMonth : c.total
          return (
            <li key={c.category}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className="truncate">{c.category}</span>
                <span className="tnum shrink-0 font-medium">{money(v)}</span>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <div
                  className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--color-plane)]"
                  role="img"
                  aria-label={`${c.category}: ${money(v)}, ${percent(c.share, 1)} of spending`}
                >
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max((v / max) * 100, 1.5)}%`,
                      background: "var(--color-series-1)",
                    }}
                  />
                </div>
                <span className="tnum w-10 shrink-0 text-right text-xs text-[var(--color-ink-muted)]">
                  {percent(c.share, 0)}
                </span>
              </div>
            </li>
          )
        })}
      </ul>
      {hidden > 0 && (
        <p className="mt-3 text-xs text-[var(--color-ink-muted)]">
          {hidden} smaller categor{hidden === 1 ? "y" : "ies"} not shown — open the table
          for all of them.
        </p>
      )}
    </ChartFrame>
  )
}
