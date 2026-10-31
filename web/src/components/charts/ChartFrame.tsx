import { Table2 } from "lucide-react"
import * as React from "react"

import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableWrap,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

export interface LegendItem {
  label: string
  color: string
}

/**
 * The chrome around every chart: title, legend, and a table view.
 *
 * The table is not a nicety. Several palette slots sit below 3:1 against the
 * light surface, and the rule that comes with that is relief — the values have
 * to be readable without relying on the colour. It also covers the case colour
 * cannot: a screen reader, a printout, forced-colours mode.
 */
export function ChartFrame({
  title,
  subtitle,
  legend,
  tableHead,
  tableRows,
  children,
  className,
  action,
}: {
  title: string
  subtitle?: React.ReactNode
  legend?: LegendItem[]
  tableHead?: string[]
  tableRows?: (string | number)[][]
  children: React.ReactNode
  className?: string
  action?: React.ReactNode
}) {
  const [showTable, setShowTable] = React.useState(false)
  const hasTable = Boolean(tableHead && tableRows?.length)

  return (
    <section
      className={cn(
        "rounded-[var(--radius-card)] border border-[var(--color-hairline)] bg-[var(--color-surface)]",
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 p-5 pb-2">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
          {subtitle && (
            <p className="mt-0.5 text-xs text-[var(--color-ink-secondary)]">{subtitle}</p>
          )}
        </div>
        <div className="flex items-center gap-1">
          {action}
          {hasTable && (
            <Button
              variant="ghost"
              size="icon"
              aria-pressed={showTable}
              aria-label={showTable ? "Show the chart" : "Show the numbers as a table"}
              title={showTable ? "Show the chart" : "Show the numbers as a table"}
              onClick={() => setShowTable((s) => !s)}
              className={cn(showTable && "bg-[var(--color-plane)]")}
            >
              <Table2 />
            </Button>
          )}
        </div>
      </header>

      {/* Legend for two or more series; a single series is named by the title. */}
      {legend && legend.length > 1 && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 px-5 pb-1">
          {legend.map((l) => (
            <li
              key={l.label}
              className="flex items-center gap-1.5 text-xs text-[var(--color-ink-secondary)]"
            >
              <span
                aria-hidden
                className="size-2.5 shrink-0 rounded-[2px]"
                style={{ background: l.color }}
              />
              {l.label}
            </li>
          ))}
        </ul>
      )}

      <div className="p-5 pt-2">
        {showTable && hasTable ? (
          <TableWrap className="max-h-80 overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  {tableHead!.map((h, i) => (
                    <TableHead key={h} className={i > 0 ? "text-right" : undefined}>
                      {h}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {tableRows!.map((r, i) => (
                  <TableRow key={i}>
                    {r.map((c, j) => (
                      <TableCell
                        key={j}
                        className={j > 0 ? "tnum text-right" : "font-medium"}
                      >
                        {c}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableWrap>
        ) : (
          children
        )}
      </div>
    </section>
  )
}

/** The shared tooltip body, so every chart's hover looks identical. */
export function ChartTooltip({
  label,
  rows,
  note,
}: {
  label: string
  rows: { label: string; value: string; color?: string }[]
  note?: string
}) {
  return (
    <div className="rounded-md border border-[var(--color-hairline)] bg-[var(--color-surface)] px-2.5 py-2 text-xs shadow-lg">
      <p className="font-medium">{label}</p>
      <ul className="mt-1 space-y-0.5">
        {rows.map((r) => (
          <li key={r.label} className="flex items-center gap-2">
            {r.color && (
              <span
                aria-hidden
                className="size-2 shrink-0 rounded-[2px]"
                style={{ background: r.color }}
              />
            )}
            <span className="text-[var(--color-ink-secondary)]">{r.label}</span>
            <span className="tnum ml-auto font-medium">{r.value}</span>
          </li>
        ))}
      </ul>
      {note && <p className="mt-1.5 text-[var(--color-ink-muted)]">{note}</p>}
    </div>
  )
}
