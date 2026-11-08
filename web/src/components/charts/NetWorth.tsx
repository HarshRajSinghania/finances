import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

import { ChartFrame, ChartTooltip } from "@/components/charts/ChartFrame"
import { dateLabel, money, moneyCompact, monthLabel, monthShort, signedMoney } from "@/lib/format"
import type { NetWorth as NetWorthData } from "@/lib/types"

const KIND = { bank: "Bank", card: "Card", investment: "Investments" } as const

/**
 * What you own minus what you owe, from the balance each statement closed on.
 *
 * One line, one axis. A month missing an account (before its first statement)
 * or carrying an old balance is said to be so in the tooltip rather than
 * smoothed over, and the accounts underneath show what each figure rests on.
 */
export function NetWorth({ data }: { data: NetWorthData }) {
  const { months, accounts, latest } = data
  if (!latest) return null
  const first = months.find((m) => !m.partial) ?? months[0]
  const change = latest.net - first.net

  return (
    <ChartFrame
      title="Net worth"
      subtitle={
        <>
          <span className="tnum text-base font-semibold text-[var(--color-ink)]">
            {money(latest.net)}
          </span>{" "}
          {months.length > 1 && (
            <>
              {signedMoney(change)} since {monthLabel(first.month)}.{" "}
            </>
          )}
          From the balance each statement closed on; cards count against it.
        </>
      }
      tableHead={["Month", "Own", "Owe", "Net worth"]}
      tableRows={months.map((m) => [
        monthLabel(m.month),
        money(m.assets),
        money(m.debts),
        money(m.net),
      ])}
    >
      {months.length > 1 && (
        <div className="h-44">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={months} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--color-hairline)" />
              <XAxis
                dataKey="month"
                tickFormatter={monthShort}
                tickLine={false}
                axisLine={false}
                fontSize={11}
                stroke="var(--color-ink-muted)"
              />
              <YAxis
                tickFormatter={moneyCompact}
                tickLine={false}
                axisLine={false}
                width={56}
                fontSize={11}
                stroke="var(--color-ink-muted)"
              />
              <Tooltip
                cursor={{ stroke: "var(--color-baseline)" }}
                content={({ active, payload }) => {
                  const m = active ? (payload?.[0]?.payload as NetWorthData["months"][number]) : null
                  if (!m) return null
                  const notes = [
                    m.partial && "Some accounts have no statement yet this month.",
                    m.stale.length > 0 && `Older balance for ${m.stale.join(", ")}.`,
                  ].filter(Boolean)
                  return (
                    <ChartTooltip
                      label={monthLabel(m.month)}
                      rows={[
                        { label: "Own", value: money(m.assets) },
                        { label: "Owe", value: money(m.debts) },
                        { label: "Net worth", value: money(m.net) },
                      ]}
                      note={notes.join(" ") || undefined}
                    />
                  )
                }}
              />
              <Area
                type="monotone"
                dataKey="net"
                stroke="var(--color-series-1)"
                strokeWidth={2}
                fill="var(--color-series-1)"
                fillOpacity={0.08}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      <ul className="mt-3 divide-y divide-[var(--color-hairline)] border-t border-[var(--color-hairline)]">
        {accounts.map((a) => (
          <li
            key={`${a.issuer}-${a.last4}-${a.kind}`}
            className="flex items-baseline gap-3 py-2 text-sm"
          >
            <span className="min-w-0 flex-1 truncate">
              {a.issuer}
              {a.last4 && <span className="tnum text-[var(--color-ink-muted)]"> ····{a.last4}</span>}
            </span>
            <span className="text-xs text-[var(--color-ink-muted)]">
              {KIND[a.kind]} · {dateLabel(a.asOf)}
            </span>
            <span className="tnum w-28 text-right font-medium">
              {money(a.kind === "card" ? -a.balance : a.balance, true)}
            </span>
          </li>
        ))}
      </ul>
    </ChartFrame>
  )
}
