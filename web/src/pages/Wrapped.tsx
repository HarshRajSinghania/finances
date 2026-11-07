/**
 * Wrapped — one year of money, framed so you might actually look at it.
 *
 * The same numbers as everywhere else, chosen for being worth a screenshot:
 * the merchant you could not stay away from, the month that got away, what
 * the subscriptions quietly took. Every tile is a plain count or sum the API
 * computed, and the page ends by saying how much of it traces to a statement
 * that reconciles — the one claim this project exists to make.
 */

import {
  CalendarDays,
  CalendarHeart,
  Coffee,
  Repeat,
  ShieldCheck,
  Sparkles,
  Store,
  TrendingDown,
  TrendingUp,
  Trophy,
} from "lucide-react"
import * as React from "react"

import { ActivityHeatmap } from "@/components/easyui/ActivityHeatmap"
import { AnimatedNumber } from "@/components/easyui/AnimatedNumber"
import { AnimatedTabs } from "@/components/easyui/AnimatedTabs"
import { Skeleton } from "@/components/ui/misc"
import { EmptyState, ErrorState } from "@/components/ui/state"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import { dateLabel, money, monthShort } from "@/lib/format"
import type { Wrapped as WrappedData } from "@/lib/types"
import { cn } from "@/lib/utils"

const SERIES = [
  "var(--color-series-1)",
  "var(--color-series-2)",
  "var(--color-series-3)",
  "var(--color-series-4)",
  "var(--color-series-5)",
]

function monthName(m: string | undefined): string {
  if (!m) return "—"
  const [y, mo] = m.split("-")
  return new Date(Number(y), Number(mo) - 1, 1).toLocaleDateString("en-US", {
    month: "long",
  })
}

export function Wrapped() {
  const [year, setYear] = React.useState<number | undefined>(undefined)
  const { data, error, loading, refetch } = useApi(() => api.wrapped(year), [year])

  if (error && !data) {
    return (
      <div className="card">
        <ErrorState error={error} onRetry={refetch} />
      </div>
    )
  }
  if (!data) return <LoadingView />

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <YearPicker
        years={data.years}
        current={data.year}
        busy={loading}
        onPick={(y) => setYear(y)}
      />
      {data.empty ? (
        <div className="card">
          <EmptyState
            icon={CalendarHeart}
            title={data.year ? `Nothing spent in ${data.year}` : "No spending yet"}
            body="Wrapped needs a year with purchases in it. Add statements or bank exports and come back."
          />
        </div>
      ) : (
        <WrappedView w={data} />
      )}
    </div>
  )
}

function YearPicker({
  years,
  current,
  busy,
  onPick,
}: {
  years: number[]
  current: number | null
  busy: boolean
  onPick: (y: number) => void
}) {
  if (years.length <= 1) return null
  return (
    <div aria-label="Year" aria-busy={busy}>
      <AnimatedTabs
        variant="pill"
        renderContent={false}
        layoutId="wrapped-year"
        tabs={years.map((y) => ({ id: String(y), label: String(y) }))}
        activeTab={current != null ? String(current) : undefined}
        onChange={(id) => onPick(Number(id))}
      />
    </div>
  )
}

function WrappedView({ w }: { w: WrappedData }) {
  const months = w.months ?? []
  const span = w.fullYear
    ? `the whole of ${w.year}`
    : `${monthName(months[0])} – ${monthName(months[months.length - 1])} ${w.year}`
  const peak = Math.max(...(w.byMonth ?? []).map((m) => m.total), 1)
  const vs = w.vsLastYear

  return (
    <div className="space-y-4">
      {/* ---------------------------------------------------------- hero */}
      <section className="relative overflow-hidden rounded-[var(--radius-card)] border border-[var(--color-hairline)] bg-[var(--color-raised)] p-6 sm:p-8">
        <div
          aria-hidden
          className="pointer-events-none absolute -top-24 -right-24 size-72 rounded-full opacity-25 blur-3xl"
          style={{
            background:
              "radial-gradient(circle, var(--color-series-1), var(--color-series-5) 60%, transparent 70%)",
          }}
        />
        <p className="relative flex items-center gap-1.5 text-xs font-medium tracking-wide text-[var(--color-ink-secondary)] uppercase">
          <Sparkles className="size-3.5" strokeWidth={1.75} aria-hidden />
          Your {w.year} in money
        </p>
        <p className="tnum relative mt-3 text-5xl font-semibold tracking-tight sm:text-6xl">
          <AnimatedNumber
            key={w.year}
            value={Math.round(w.total ?? 0)}
            prefix={money(0).replace(/[0-9.,\s]/g, "")}
            className="font-sans text-[var(--color-ink)]"
          />
        </p>
        <p className="relative mt-2 max-w-xl text-sm leading-relaxed text-[var(--color-ink-secondary)]">
          spent across {w.transactions?.toLocaleString()} purchases at {w.merchants} places
          over {span} — about{" "}
          <span className="tnum font-medium text-[var(--color-ink)]">
            {money(w.perDay, true)}
          </span>{" "}
          a day.
        </p>
        {w.income != null && w.kept != null && (
          <p className="relative mt-4 inline-flex flex-wrap gap-x-4 gap-y-1 rounded-md bg-[var(--color-plane)] px-3 py-2 text-xs">
            <span>
              Earned <span className="tnum font-medium">{money(w.income)}</span>
            </span>
            <span>
              {w.kept >= 0 ? "Kept" : "Overspent"}{" "}
              <span
                className={cn(
                  "tnum font-medium",
                  w.kept >= 0 ? "text-[var(--color-success-text)]" : "text-[var(--color-critical)]",
                )}
              >
                {money(Math.abs(w.kept))}
              </span>
            </span>
          </p>
        )}

        {/* month strip */}
        {w.byMonth && w.byMonth.length > 1 && (
          <div className="relative mt-6 flex h-16 items-end gap-1" aria-hidden>
            {w.byMonth.map((m) => (
              <div key={m.month} className="flex flex-1 flex-col items-center gap-1">
                <div
                  className={cn(
                    "w-full rounded-sm",
                    m.month === w.biggestMonth?.month
                      ? "bg-[var(--color-series-2)]"
                      : "bg-[var(--color-series-1)]/60",
                  )}
                  style={{ height: `${Math.max((m.total / peak) * 48, 2)}px` }}
                />
                <span className="text-[10px] text-[var(--color-ink-muted)]">
                  {monthShort(m.month).slice(0, 1)}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ------------------------------------------------ every day */}
      {w.byDay && w.byDay.length > 0 && (
        <section className="card p-5 sm:p-6">
          <h2 className="text-sm font-semibold tracking-tight">Every day of {w.year}</h2>
          <p className="mt-1 mb-4 text-xs text-[var(--color-ink-secondary)]">
            One square a day, darker the more you spent. Hover or tap a day for its total.
          </p>
          <ActivityHeatmap
            key={w.year}
            data={w.byDay.map((d) => ({ date: d.date, value: d.total }))}
            endDate={`${w.year}-12-31`}
            weeks={53}
            color="#eb6834"
            unit="spent"
            format={(n) => money(n)}
            caption={`${money(w.total ?? 0)} spent in ${w.year}`}
            ariaLabel={`Spending per day in ${w.year}`}
          />
        </section>
      )}

      {/* ------------------------------------------------- where it went */}
      <section className="card p-5 sm:p-6">
        <h2 className="text-sm font-semibold tracking-tight">Where it went</h2>
        <ul className="mt-4 space-y-3">
          {(w.topCategories ?? []).map((c, i) => (
            <li key={c.category}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className="min-w-0 truncate">{c.category}</span>
                <span className="tnum shrink-0 text-[var(--color-ink-secondary)]">
                  {money(c.total)}{" "}
                  <span className="text-xs text-[var(--color-ink-muted)]">
                    {c.share.toFixed(0)}%
                  </span>
                </span>
              </div>
              <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-[var(--color-hairline)]">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(c.share, 1)}%`,
                    background: SERIES[i % SERIES.length],
                  }}
                />
              </div>
            </li>
          ))}
        </ul>
      </section>

      {/* ---------------------------------------------------------- tiles */}
      <div className="grid gap-3 sm:grid-cols-2">
        {w.favourite && (
          <Tile icon={Coffee} label="Your regular" accent="var(--color-series-2)">
            <p className="text-xl font-semibold tracking-tight">{w.favourite.name}</p>
            <p className="tnum mt-1 text-sm text-[var(--color-ink-secondary)]">
              {w.favourite.visits} visits · {money(w.favourite.total)}
            </p>
          </Tile>
        )}
        {w.biggest && (
          <Tile icon={Trophy} label="Biggest single purchase" accent="var(--color-series-4)">
            <p className="tnum text-xl font-semibold tracking-tight">
              {money(w.biggest.amount, true)}
            </p>
            <p className="mt-1 text-sm text-[var(--color-ink-secondary)]">
              {w.biggest.name} · {dateLabel(w.biggest.date)}
            </p>
          </Tile>
        )}
        {w.biggestDay && (
          <Tile icon={CalendarDays} label="Most expensive day" accent="var(--color-series-5)">
            <p className="tnum text-xl font-semibold tracking-tight">
              {money(w.biggestDay.total)}
            </p>
            <p className="mt-1 text-sm text-[var(--color-ink-secondary)]">
              {dateLabel(w.biggestDay.date)}
              {w.weekday ? ` · you spend most on ${w.weekday}s` : ""}
            </p>
          </Tile>
        )}
        {w.biggestMonth && w.quietestMonth && (
          <Tile icon={TrendingUp} label="The month that got away" accent="var(--color-series-1)">
            <p className="text-xl font-semibold tracking-tight">
              {monthName(w.biggestMonth.month)}
            </p>
            <p className="tnum mt-1 text-sm text-[var(--color-ink-secondary)]">
              {money(w.biggestMonth.total)} · quietest was {monthName(w.quietestMonth.month)} (
              {money(w.quietestMonth.total)})
            </p>
          </Tile>
        )}
        {w.subscriptions && w.subscriptions.count > 0 && (
          <Tile icon={Repeat} label="Subscriptions" accent="var(--color-series-3)">
            <p className="tnum text-xl font-semibold tracking-tight">
              {money(w.subscriptions.paid)}
            </p>
            <p className="mt-1 text-sm text-[var(--color-ink-secondary)]">
              paid to {w.subscriptions.count} subscriptions this year
            </p>
          </Tile>
        )}
        <Tile icon={Store} label="New places" accent="var(--color-series-2)">
          <p className="tnum text-xl font-semibold tracking-tight">{w.newMerchants ?? 0}</p>
          <p className="mt-1 text-sm text-[var(--color-ink-secondary)]">
            merchants you had never paid before {w.year}
          </p>
        </Tile>
      </div>

      {/* ------------------------------------------------- top merchants */}
      {w.topMerchants && w.topMerchants.length > 0 && (
        <section className="card p-5 sm:p-6">
          <h2 className="text-sm font-semibold tracking-tight">Top places</h2>
          <ol className="mt-3 divide-y divide-[var(--color-hairline)]">
            {w.topMerchants.map((m, i) => (
              <li key={m.name} className="flex items-center gap-3 py-2.5 text-sm">
                <span className="tnum w-5 shrink-0 text-xs text-[var(--color-ink-muted)]">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{m.name}</span>
                <span className="tnum shrink-0 text-xs text-[var(--color-ink-muted)]">
                  {m.visits}×
                </span>
                <span className="tnum w-20 shrink-0 text-right">{money(m.total)}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* ------------------------------------------------- comparison */}
      {vs && (
        <section className="card flex items-start gap-3 p-5">
          {vs.pct > 0 ? (
            <TrendingUp className="mt-0.5 size-5 shrink-0 text-[var(--color-serious)]" strokeWidth={1.75} aria-hidden />
          ) : (
            <TrendingDown className="mt-0.5 size-5 shrink-0 text-[var(--color-good)]" strokeWidth={1.75} aria-hidden />
          )}
          <p className="text-sm leading-relaxed">
            Over the same {vs.months} months you spent{" "}
            <span className="tnum font-semibold">
              {Math.abs(vs.pct).toFixed(0)}% {vs.pct > 0 ? "more" : "less"}
            </span>{" "}
            than the year before — {money(vs.now)} against {money(vs.then)}.
          </p>
        </section>
      )}

      {/* ------------------------------------------------------ the claim */}
      <p className="flex items-center gap-2 px-1 text-xs text-[var(--color-ink-secondary)]">
        <ShieldCheck className="size-4 shrink-0 text-[var(--color-good)]" strokeWidth={1.75} aria-hidden />
        <span>
          <span className="tnum font-medium text-[var(--color-ink)]">
            {(w.traced ?? 0).toFixed(0)}%
          </span>{" "}
          of these purchases trace to a statement that reconciles. Read on this machine;
          nothing was uploaded.
        </span>
      </p>
    </div>
  )
}

function Tile({
  icon: Icon,
  label,
  accent,
  children,
}: {
  icon: React.ElementType
  label: string
  accent: string
  children: React.ReactNode
}) {
  return (
    <div className="card relative overflow-hidden p-5">
      <div
        aria-hidden
        className="absolute inset-y-0 left-0 w-1"
        style={{ background: accent }}
      />
      <p className="flex items-center gap-1.5 text-xs text-[var(--color-ink-secondary)]">
        <Icon className="size-3.5" strokeWidth={1.75} style={{ color: accent }} aria-hidden />
        {label}
      </p>
      <div className="mt-2 min-w-0">{children}</div>
    </div>
  )
}

function LoadingView() {
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <Skeleton className="h-56 w-full rounded-[var(--radius-card)]" />
      <Skeleton className="h-48 w-full rounded-[var(--radius-card)]" />
      <div className="grid gap-3 sm:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-[var(--radius-card)]" />
        ))}
      </div>
    </div>
  )
}
