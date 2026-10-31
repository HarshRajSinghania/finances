import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import { ChartFrame, ChartTooltip } from "@/components/charts/ChartFrame"
import { money, moneyCompact, monthLabel, monthShort } from "@/lib/format"
import type { MonthRow } from "@/lib/types"

/**
 * Spend per month, with income as a second series when it is known.
 *
 * Two measures on one axis, both in dollars — never a second y-scale. Partial
 * months are drawn hollow rather than dropped: hiding them would misrepresent
 * the record, and drawing them solid would imply a month that never finished
 * was a normal one.
 */
export function MonthlyBars({
  months,
  showIncome = true,
}: {
  months: MonthRow[]
  showIncome?: boolean
}) {
  const hasIncome = showIncome && months.some((m) => m.income > 0)
  const incomplete = months.filter((m) => !m.complete).length

  const legend = hasIncome
    ? [
        { label: "Spending", color: "var(--color-series-1)" },
        { label: "Income", color: "var(--color-series-3)" },
      ]
    : [{ label: "Spending", color: "var(--color-series-1)" }]

  return (
    <ChartFrame
      title={hasIncome ? "Spending and income by month" : "Spending by month"}
      subtitle={
        incomplete > 0
          ? `${incomplete} partial month${incomplete === 1 ? "" : "s"} shown hollow — the data does not cover all of it, so it is left out of averages.`
          : "Every month shown is fully covered by the statements."
      }
      legend={legend}
      tableHead={hasIncome ? ["Month", "Spending", "Income", "Net"] : ["Month", "Spending"]}
      tableRows={months.map((m) =>
        hasIncome
          ? [
              monthLabel(m.month) + (m.complete ? "" : " (partial)"),
              money(m.spend),
              money(m.income),
              money(m.net),
            ]
          : [monthLabel(m.month) + (m.complete ? "" : " (partial)"), money(m.spend)],
      )}
    >
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={months}
            margin={{ top: 4, right: 4, bottom: 0, left: -8 }}
            barGap={2}
            barCategoryGap="22%"
          >
            <CartesianGrid
              vertical={false}
              stroke="var(--color-hairline)"
              strokeDasharray="0"
            />
            <XAxis
              dataKey="month"
              tickFormatter={monthShort}
              tickLine={false}
              axisLine={{ stroke: "var(--color-baseline)" }}
              tick={{ fill: "var(--color-ink-muted)", fontSize: 11 }}
              interval="preserveStartEnd"
            />
            <YAxis
              tickFormatter={moneyCompact}
              tickLine={false}
              axisLine={false}
              width={56}
              tick={{ fill: "var(--color-ink-muted)", fontSize: 11 }}
            />
            <Tooltip
              cursor={{ fill: "var(--color-plane)" }}
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) return null
                const row = payload[0]!.payload as MonthRow
                return (
                  <ChartTooltip
                    label={monthLabel(String(label))}
                    rows={[
                      {
                        label: "Spending",
                        value: money(row.spend),
                        color: "var(--color-series-1)",
                      },
                      ...(hasIncome
                        ? [
                            {
                              label: "Income",
                              value: money(row.income),
                              color: "var(--color-series-3)",
                            },
                            { label: "Net", value: money(row.net) },
                          ]
                        : []),
                    ]}
                    note={row.complete ? undefined : "Partial month — not in averages."}
                  />
                )
              }}
            />
            <Bar dataKey="spend" radius={[4, 4, 0, 0]} isAnimationActive={false}>
              {months.map((m) => (
                <Cell
                  key={m.month}
                  fill={m.complete ? "var(--color-series-1)" : "transparent"}
                  stroke="var(--color-series-1)"
                  strokeWidth={m.complete ? 0 : 1.5}
                  strokeDasharray={m.complete ? undefined : "3 2"}
                />
              ))}
            </Bar>
            {hasIncome && (
              <Bar dataKey="income" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {months.map((m) => (
                  <Cell
                    key={m.month}
                    fill={m.complete ? "var(--color-series-3)" : "transparent"}
                    stroke="var(--color-series-3)"
                    strokeWidth={m.complete ? 0 : 1.5}
                    strokeDasharray={m.complete ? undefined : "3 2"}
                  />
                ))}
              </Bar>
            )}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </ChartFrame>
  )
}
