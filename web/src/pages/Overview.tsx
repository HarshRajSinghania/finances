import {
  ArrowRight,
  CalendarClock,
  CalendarOff,
  CircleAlert,
  CircleCheck,
  CircleHelp,
  CircleSlash,
  FileUp,
  Loader2,
  Repeat,
  TrendingDown,
  TrendingUp,
} from "lucide-react"
import * as React from "react"
import { Link } from "react-router-dom"

import { CategoryBars } from "@/components/charts/CategoryBars"
import { MonthlyBars } from "@/components/charts/MonthlyBars"
import { NetWorth } from "@/components/charts/NetWorth"
import { StatCard, StatGrid } from "@/components/easyui/StatCard"
import { Stat } from "@/components/Stat"
import { ReadyReminder } from "@/components/StatementSources"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress, Separator, Skeleton } from "@/components/ui/misc"
import { EmptyState, ErrorState, LoadingRows } from "@/components/ui/state"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import {
  cadenceLabel,
  dateLabel,
  fileLabel,
  money,
  percent,
  relativeDay,
  signedMoney,
  signedPercent,
} from "@/lib/format"
import type { Coverage, MonthRow, PaymentDue, Payments, Recurring, Summary } from "@/lib/types"

/**
 * The landing screen.
 *
 * Ordered by what a reader has to know first. The reconciliation verdict leads,
 * because every figure under it is only worth as much as the answer to "can
 * these numbers be traced back to a statement?" — a dashboard that shows the
 * averages first and the caveats last has already told the reader to trust
 * something it has not yet justified.
 */
export function Overview({
  summary,
  loading,
}: {
  summary: Summary | null
  loading: boolean
}) {
  // The summary is fetched once in App and handed down; everything else this
  // page needs is its own request, each with its own three states.
  const months = useApi(() => api.months(), [])
  const categories = useApi(() => api.categories(), [])
  const recurring = useApi(() => api.recurring(), [])
  const payments = useApi(() => api.payments(), [])
  const networth = useApi(() => api.networth(), [])
  const sources = useApi(() => api.sources(), [])

  if (!summary) {
    // App owns the fatal case (the API is not running) and renders the error
    // screen itself, so the only state left here is the first load.
    return loading ? <OverviewSkeleton /> : null
  }

  if (summary.coverage.statements === 0) {
    return (
      <Card>
        <EmptyState
          icon={FileUp}
          title="No statements have been read yet"
          body="This dashboard is computed entirely from statement PDFs on this machine. Add a few and every figure here will trace back to a line one of them prints."
          action={
            <Button asChild size="sm">
              <Link to="/statements">
                Add statements
                <ArrowRight />
              </Link>
            </Button>
          }
        />
      </Card>
    )
  }

  const completeMonths = summary.completeMonths.length

  return (
    <div className="space-y-6">
      <TrustPanel coverage={summary.coverage} />

      {sources.data && <ReadyReminder data={sources.data} />}

      {payments.data && (payments.data.payments.length > 0 || payments.data.unread > 0) && (
        <CardPayments data={payments.data} onRead={payments.refetch} />
      )}

      {months.data && <LatestMonth months={months.data.months} />}

      {networth.data && networth.data.latest && <NetWorth data={networth.data} />}

      <section aria-labelledby="headline-stats" className="space-y-3">
        <h2 id="headline-stats" className="sr-only">
          Headline figures
        </h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          <Stat
            label="Average month"
            value={money(summary.monthlyAverage)}
            emphasis
            sub={
              completeMonths > 0
                ? `Across ${completeMonths} complete month${completeMonths === 1 ? "" : "s"}.`
                : "No month is fully covered yet."
            }
            hint="Total spending divided by the number of months the statements cover end to end. Partial months are left out — counting a half month whole would drag the average down."
          />

          <Stat
            label="Income a month"
            value={
              summary.incomeAverage === null ? (
                <span className="text-xl text-[var(--color-ink-secondary)]">
                  Not identified
                </span>
              ) : (
                money(summary.incomeAverage)
              )
            }
            sub={
              summary.incomeAverage === null
                ? "No deposits in these statements look like pay."
                : `${money(summary.totalCredits)} came in over the whole period, including transfers and card payments.`
            }
            hint="Deposits that repeat at a steady interval and a steady size. Card payments and internal transfers are not counted as income."
          />

          <SurplusStat summary={summary} completeMonths={completeMonths} />

          <Stat
            label="Essential spending"
            value={money(summary.essentialMonthly)}
            sub={`${percent(share(summary.essentialMonthly, summary.monthlyAverage), 0)} of the average month.`}
            hint="The monthly average of the categories that are hard to stop: housing, utilities, phone and internet, groceries, transport, fuel, insurance, health, pharmacy, education, childcare and tax. It is what an emergency fund has to cover."
          />

          <Stat
            label="Committed a month"
            value={money(summary.committedMonthly)}
            sub={`${summary.subscriptionCount} charge${summary.subscriptionCount === 1 ? "" : "s"} that repeat on a schedule.`}
            hint="Every charge that recurs on a detected cadence — subscriptions, memberships, instalments, insurance — converted to a monthly figure. Weekly and yearly charges are pro-rated, so this is what repeats whether or not you spend anything else."
          />
        </div>
      </section>

      <section aria-label="Spending by month">
        {months.loading && !months.data ? (
          <Card>
            <LoadingRows rows={4} />
          </Card>
        ) : months.error ? (
          <Card>
            <ErrorState error={months.error} onRetry={months.refetch} />
          </Card>
        ) : months.data && months.data.months.length > 0 ? (
          <MonthlyBars months={months.data.months} />
        ) : (
          <Card>
            <EmptyState
              title="No month has any activity"
              body="The statements were read, but no dated transactions came out of them."
            />
          </Card>
        )}
      </section>

      <div className="grid gap-6 xl:grid-cols-2">
        <section aria-label="Spending by category">
          {categories.loading && !categories.data ? (
            <Card>
              <LoadingRows rows={6} />
            </Card>
          ) : categories.error ? (
            <Card>
              <ErrorState error={categories.error} onRetry={categories.refetch} />
            </Card>
          ) : categories.data && categories.data.categories.length > 0 ? (
            <CategoryBars categories={categories.data.categories} limit={8} />
          ) : (
            <Card>
              <EmptyState
                title="Nothing to categorise"
                body="No spending was found in these statements."
              />
            </Card>
          )}
        </section>

        <section aria-label="Recurring charges">
          {recurring.loading && !recurring.data ? (
            <Card>
              <LoadingRows rows={6} />
            </Card>
          ) : recurring.error ? (
            <Card>
              <ErrorState error={recurring.error} onRetry={recurring.refetch} />
            </Card>
          ) : (
            <RecurringPanel
              rows={recurring.data?.recurring ?? []}
              committedMonthly={recurring.data?.committedMonthly ?? 0}
            />
          )}
        </section>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ trust panel */

/**
 * The reconciliation verdict, at full width, above everything else.
 *
 * Three outcomes, kept apart on purpose. A statement that disagrees with its own
 * printed total is a defect; a statement that printed no total to compare
 * against is an absence of evidence, not evidence of a problem; a month with no
 * statement at all is neither, and it is the one that silently corrupts every
 * average further down the page.
 */
function TrustPanel({ coverage }: { coverage: Coverage }) {
  const { statements, reconciled, mismatched, unchecked, pct, gaps, mismatches, trusted } =
    coverage

  const known = mismatches.filter((m) => m.diff !== null)
  const totalGap = known.reduce((s, m) => s + Math.abs(m.diff ?? 0), 0)
  const worst = [...mismatches]
    .sort((a, b) => Math.abs(b.diff ?? 0) - Math.abs(a.diff ?? 0))
    .slice(0, 3)

  const Icon = trusted ? CircleCheck : mismatched > 0 ? CircleAlert : CircleHelp
  const tone = trusted
    ? "var(--color-good)"
    : mismatched > 0
      ? "var(--color-critical)"
      : "var(--color-warning)"

  const headline = trusted
    ? `All ${statements} statement${statements === 1 ? "" : "s"} reconcile against their own printed totals.`
    : `${reconciled} of ${statements} statements reconcile against their own printed totals.`

  return (
    <Card className="overflow-hidden" role="region" aria-label="Reconciliation status">
      <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <Icon
            className="mt-0.5 size-5 shrink-0"
            style={{ color: tone }}
            strokeWidth={1.75}
            aria-hidden
          />
          <div className="min-w-0 space-y-1">
            <h2 className="text-sm font-semibold tracking-tight">{headline}</h2>
            <p className="max-w-prose text-xs leading-relaxed text-[var(--color-ink-secondary)]">
              {trusted
                ? "Every figure below traces to a statement line. Nothing here is estimated, inferred or filled in."
                : "Figures below are computed from what was extracted. Where a statement could not be squared against its own totals, treat the numbers it contributes as unconfirmed."}
            </p>
          </div>
        </div>

        <dl className="flex shrink-0 gap-5 sm:justify-end">
          <Tally label="Reconciled" value={reconciled} />
          <Tally label="Mismatched" value={mismatched} />
          <Tally label="Unchecked" value={unchecked} />
        </dl>
      </div>

      {!trusted && (
        <div className="px-5 pb-4">
          <Progress
            value={pct}
            aria-label={`${percent(pct, 0)} of statements reconciled`}
            indicatorClassName={
              mismatched > 0
                ? "bg-[var(--color-critical)]"
                : "bg-[var(--color-warning)]"
            }
          />
          <p className="mt-1.5 text-xs text-[var(--color-ink-muted)]">
            {percent(pct, 0)} verified.
          </p>
        </div>
      )}

      {(mismatched > 0 || unchecked > 0 || gaps.length > 0) && (
        <>
          <Separator />
          <ul className="divide-y divide-[var(--color-hairline)]">
            {mismatched > 0 && (
              <Finding
                icon={CircleAlert}
                tone="critical"
                label="Do not add up"
                title={`${mismatched} statement${mismatched === 1 ? "" : "s"} disagree with the totals ${mismatched === 1 ? "it prints" : "they print"}${
                  known.length > 0
                    ? `, by ${money(totalGap, totalGap < 100)} in total`
                    : ""
                }.`}
                body="The extraction and the printed summary do not match, so charges are either missing or double-counted. Every average on this page includes them."
              >
                <ul className="mt-2 space-y-1">
                  {worst.map((m) => (
                    <li
                      key={m.sourceFile}
                      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-xs"
                    >
                      <span className="min-w-0 truncate text-[var(--color-ink-secondary)]">
                        {m.issuer} — {fileLabel(m.sourceFile)}
                        {m.note && (
                          <span className="text-[var(--color-ink-muted)]"> · {m.note}</span>
                        )}
                      </span>
                      <span className="tnum shrink-0 font-medium">
                        {m.diff === null ? "amount unknown" : signedMoney(m.diff, true)}
                      </span>
                    </li>
                  ))}
                  {mismatches.length > worst.length && (
                    <li className="text-xs text-[var(--color-ink-muted)]">
                      and {mismatches.length - worst.length} more.
                    </li>
                  )}
                </ul>
              </Finding>
            )}

            {unchecked > 0 && (
              <Finding
                icon={CircleHelp}
                tone="warning"
                label="Nothing to check against"
                title={`${unchecked} statement${unchecked === 1 ? "" : "s"} printed no total to compare against.`}
                body="This is not the same as being wrong. The transactions were read normally; the statement simply carries no summary figure, so there is no independent number to square them with."
              />
            )}

            {gaps.length > 0 && (
              <Finding
                icon={CalendarOff}
                tone="warning"
                label="Missing months"
                title={`${gaps.length} gap${gaps.length === 1 ? "" : "s"} in the statement history.`}
                body="Those months are absent entirely, not empty. Every average above is computed on incomplete data and is lower than the truth by whatever was spent in them."
              >
                <ul className="mt-2 space-y-1">
                  {gaps.slice(0, 3).map((g) => (
                    <li
                      key={`${g.account}-${g.after}`}
                      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-xs"
                    >
                      <span className="min-w-0 truncate text-[var(--color-ink-secondary)]">
                        {g.account} — nothing between {dateLabel(g.after)} and{" "}
                        {dateLabel(g.before)}
                      </span>
                      <span className="tnum shrink-0 font-medium">{g.days} days</span>
                    </li>
                  ))}
                  {gaps.length > 3 && (
                    <li className="text-xs text-[var(--color-ink-muted)]">
                      and {gaps.length - 3} more.
                    </li>
                  )}
                </ul>
              </Finding>
            )}
          </ul>
        </>
      )}

      <CardFooter className="justify-between gap-3">
        <p className="text-xs text-[var(--color-ink-muted)]">
          {trusted
            ? "Checked against each statement's own printed totals, on this machine."
            : "Each statement is checked against the totals it prints, on this machine."}
        </p>
        <Button asChild variant="outline" size="sm">
          <Link to="/statements">
            {trusted ? "See the statements" : "Review the statements"}
            <ArrowRight />
          </Link>
        </Button>
      </CardFooter>
    </Card>
  )
}

function Tally({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs whitespace-nowrap text-[var(--color-ink-muted)]">{label}</dt>
      <dd className="tnum mt-0.5 text-lg font-semibold tracking-tight">{value}</dd>
    </div>
  )
}

function Finding({
  icon: Icon,
  tone,
  label,
  title,
  body,
  children,
}: {
  icon: React.ElementType
  tone: "critical" | "warning"
  label: string
  title: string
  body: string
  children?: React.ReactNode
}) {
  // Colour never carries this on its own: the icon and the badge text say it too.
  const color = tone === "critical" ? "var(--color-critical)" : "var(--color-warning)"
  return (
    <li className="flex gap-3 px-5 py-4">
      <Icon
        className="mt-0.5 size-4 shrink-0"
        style={{ color }}
        strokeWidth={1.75}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium">{title}</p>
          <Badge variant={tone === "critical" ? "critical" : "warning"}>{label}</Badge>
        </div>
        <p className="mt-1 max-w-prose text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          {body}
        </p>
        {children}
      </div>
    </li>
  )
}

/* -------------------------------------------------------------- surplus */

/**
 * Income minus spending. Kept separate because it has a third state most stat
 * tiles do not: it can be unknowable. Showing $0 when no income was found would
 * be a claim the data does not support.
 */
function SurplusStat({
  summary,
  completeMonths,
}: {
  summary: Summary
  completeMonths: number
}) {
  const { surplus } = summary

  if (surplus === null) {
    return (
      <Stat
        label="Left over each month"
        value={
          <span className="text-xl text-[var(--color-ink-secondary)]">Income unknown</span>
        }
        sub="Nothing in these statements was identified as income, so there is nothing to subtract spending from."
        hint="Income a month minus spending a month. Card statements often show no pay at all — a deposit only appears if the account it lands in was included."
      />
    )
  }

  const short = surplus < 0
  return (
    <Stat
      label={short ? "Short each month" : "Left over each month"}
      value={signedMoney(surplus)}
      emphasis={short}
      sub={
        short ? (
          <>
            <Badge variant="serious">
              <TrendingDown aria-hidden />
              Spending exceeds income
            </Badge>{" "}
            <span className="align-middle">
              {money(Math.abs(surplus))} a month over{" "}
              {completeMonths || summary.monthsCovered} months, drawn from savings,
              credit, or income these statements do not show.
            </span>
          </>
        ) : (
          `Held back after ${money(summary.monthlyAverage)} of spending in a typical month.`
        )
      }
      hint="Identified income a month minus spending a month, averaged over the complete months only. It is a measured figure, not a budget."
    />
  )
}

/* ------------------------------------------------------------- recurring */

/**
 * What repeats. The interesting part is not the list but the two exceptions:
 * charges that stopped arriving and are still counted, and charges whose price
 * has moved since they started.
 */
function RecurringPanel({
  rows,
  committedMonthly,
}: {
  rows: Recurring[]
  committedMonthly: number
}) {
  if (rows.length === 0) {
    return (
      <Card className="h-full">
        <CardHeader>
          <CardTitle>Recurring charges</CardTitle>
        </CardHeader>
        <EmptyState
          icon={Repeat}
          title="Nothing repeats on a schedule yet"
          body="A charge is only called recurring once it has appeared enough times at a steady interval and a steady price. A few more months of statements will settle it."
        />
      </Card>
    )
  }

  const perYear = rows.reduce((s, r) => s + r.perYear, 0)
  const stale = rows.filter((r) => r.isStale)
  const staleMonthly = stale.reduce((s, r) => s + r.perMonth, 0)
  const drift = rows
    .filter((r) => !r.isStale && Math.abs(r.pricePct) >= 10 && r.n >= 3)
    .sort((a, b) => Math.abs(b.pricePct) - Math.abs(a.pricePct))
    .slice(0, 2)
  const top = [...rows].sort((a, b) => b.perMonth - a.perMonth).slice(0, 4)

  return (
    <Card className="flex h-full flex-col">
      <CardHeader>
        <CardTitle>Recurring charges</CardTitle>
        <p className="text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          {rows.length} charge{rows.length === 1 ? "" : "s"} repeat on a detected
          cadence — {money(committedMonthly)} a month, {money(perYear)} a year. Weekly
          and yearly charges are pro-rated to a month.
        </p>
      </CardHeader>

      <CardContent className="flex-1 space-y-4">
        <div>
          <h3 className="text-xs font-medium text-[var(--color-ink-secondary)]">
            The largest
          </h3>
          <ul className="mt-2 space-y-2">
            {top.map((r) => (
              <li key={r.key} className="flex items-baseline justify-between gap-3">
                <span className="min-w-0">
                  <span className="block truncate text-sm">{r.name}</span>
                  <span className="block truncate text-xs text-[var(--color-ink-muted)]">
                    {cadenceLabel[r.cadence] ?? r.cadence} · {money(r.amount, true)} a
                    time · {r.n} seen · last {relativeDay(r.lastSeen)}
                  </span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="tnum block text-sm font-medium">
                    {money(r.perMonth)}
                  </span>
                  <span className="tnum block text-xs text-[var(--color-ink-muted)]">
                    /mo
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>

        {(stale.length > 0 || drift.length > 0) && (
          <>
            <Separator />
            <div className="space-y-3">
              {stale.length > 0 && (
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="warning">
                      <CircleSlash aria-hidden />
                      Overdue
                    </Badge>
                    <p className="text-sm font-medium">
                      {stale.length} charge{stale.length === 1 ? " is" : "s are"} well
                      past due
                    </p>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-[var(--color-ink-secondary)]">
                    {money(staleMonthly)} a month of the {money(committedMonthly)} above
                    belongs to {stale.length === 1 ? "a charge" : "charges"} that stopped
                    arriving — cancelled, or a card that expired.{" "}
                    {stale
                      .slice(0, 3)
                      .map((r) => r.name)
                      .join(", ")}
                    {stale.length > 3 ? `, and ${stale.length - 3} more.` : "."}
                  </p>
                </div>
              )}

              {drift.length > 0 && (
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="neutral">
                      {drift[0]!.pricePct > 0 ? (
                        <TrendingUp aria-hidden />
                      ) : (
                        <TrendingDown aria-hidden />
                      )}
                      Price moved
                    </Badge>
                    <p className="text-sm font-medium">
                      {drift.length === 1 ? "One charge is" : `${drift.length} charges are`}{" "}
                      not what {drift.length === 1 ? "it" : "they"} started at
                    </p>
                  </div>
                  <ul className="mt-1 space-y-1">
                    {drift.map((r) => (
                      <li
                        key={r.key}
                        className="flex flex-wrap items-baseline justify-between gap-x-3 text-xs"
                      >
                        <span className="min-w-0 truncate text-[var(--color-ink-secondary)]">
                          {r.name}
                        </span>
                        <span className="tnum shrink-0 font-medium">
                          {signedPercent(r.pricePct)} ({signedMoney(r.priceChange, true)})
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </>
        )}
      </CardContent>

      <CardFooter className="justify-end">
        <Button asChild variant="outline" size="sm">
          <Link to="/subscriptions">
            All {rows.length} recurring charges
            <ArrowRight />
          </Link>
        </Button>
      </CardFooter>
    </Card>
  )
}

/* -------------------------------------------------------------- skeleton */

/** First load. Deliberately the same shape as the real page, and never a zero. */
function OverviewSkeleton() {
  return (
    <div className="space-y-6">
      <Card>
        <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-4 w-72 max-w-full" />
            <Skeleton className="h-3 w-96 max-w-full" />
          </div>
          <div className="flex shrink-0 gap-5">
            <Skeleton className="h-10 w-16" />
            <Skeleton className="h-10 w-16" />
            <Skeleton className="h-10 w-16" />
          </div>
        </div>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {["Average month", "Income a month", "Left over each month", "Essential spending", "Committed a month"].map(
          (label) => (
            <Stat key={label} label={label} value="" loading />
          ),
        )}
      </div>

      <Card>
        <LoadingRows rows={4} />
      </Card>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <LoadingRows rows={6} />
        </Card>
        <Card>
          <LoadingRows rows={6} />
        </Card>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- helpers */

function share(part: number, whole: number): number | null {
  if (!whole) return null
  return (part / whole) * 100
}

/**
 * The newest complete month against the one before, with the run of months
 * behind it as a sparkline you can scrub. Partial months are left out: a
 * month the statements only half cover would always look like a saving.
 */
function LatestMonth({ months }: { months: MonthRow[] }) {
  const done = months.filter((m) => m.complete).slice(-12)
  if (done.length < 2) return null
  const last = done[done.length - 1]
  const prev = done[done.length - 2]
  const labels = done.map((m) => monthName(m.month))
  const vs = `vs ${monthName(prev.month)}`
  const fmt = (n: number) => money(n)
  return (
    <section aria-labelledby="latest-month" className="space-y-3">
      <h2 id="latest-month" className="text-sm font-semibold tracking-tight">
        {monthName(last.month)}, the latest full month
      </h2>
      <StatGrid>
        <StatCard
          label="Spent"
          value={last.spend}
          previous={prev.spend}
          trend={done.map((m) => m.spend)}
          trendLabels={labels}
          goodWhen="down"
          period={vs}
          format={fmt}
          color="#eb6834"
        />
        <StatCard
          label="Came in"
          value={last.income}
          previous={prev.income}
          trend={done.map((m) => m.income)}
          trendLabels={labels}
          period={vs}
          format={fmt}
          color="#2a78d6"
        />
        <StatCard
          label="Kept"
          value={last.net}
          previous={prev.net}
          trend={done.map((m) => m.net)}
          trendLabels={labels}
          period={vs}
          format={fmt}
          color="#1baf7a"
        />
      </StatGrid>
    </section>
  )
}

function monthName(ym: string) {
  return new Date(`${ym}-01T00:00:00`).toLocaleDateString("en-US", {
    month: "short",
    year: "numeric",
  })
}

/* ------------------------------------------------------- card payments */

const PAYMENT_BADGE: Record<PaymentDue["state"], { label: (p: PaymentDue) => string; variant: "good" | "warning" | "critical" | "neutral" }> = {
  paid: { label: () => "Paid in full", variant: "good" },
  "nothing due": { label: () => "Nothing due", variant: "good" },
  "minimum paid": { label: () => "Minimum paid", variant: "neutral" },
  due: {
    label: (p) => (p.daysLeft === 0 ? "Due today" : p.daysLeft === 1 ? "Due tomorrow" : `Due in ${p.daysLeft} days`),
    variant: "warning",
  },
  unpaid: { label: () => "Not paid by the due date", variant: "critical" },
  unknown: { label: () => "Due date passed", variant: "neutral" },
}

/**
 * When each card's payment is due, read from its latest statement, and what
 * the files show was paid towards it since. A payment made after the latest
 * export is not visible yet, which the footnote says rather than crying wolf.
 */
function CardPayments({ data, onRead }: { data: Payments; onRead: () => void }) {
  const { payments: rows, seenUntil, unread } = data
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CalendarClock className="size-4" strokeWidth={1.75} aria-hidden />
          Card payments
        </CardTitle>
      </CardHeader>
      <CardContent className="divide-y divide-[var(--color-hairline)] p-0">
        {unread > 0 && <ReadDueDates unread={unread} none={rows.length === 0} onRead={onRead} />}
        {rows.map((p) => {
          const badge = PAYMENT_BADGE[p.state]
          return (
            <div
              key={`${p.issuer}-${p.last4}`}
              className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-3"
            >
              <div className="min-w-[10rem] flex-1">
                <p className="text-sm font-medium">
                  {p.issuer} <span className="tnum text-[var(--color-ink-muted)]">····{p.last4}</span>
                </p>
                <p className="text-xs text-[var(--color-ink-secondary)]">
                  Due {dateLabel(p.due)} · statement closed {dateLabel(p.closed)}
                </p>
              </div>
              <dl className="flex gap-6 text-sm">
                <div>
                  <dt className="text-xs text-[var(--color-ink-muted)]">Statement balance</dt>
                  <dd className="tnum font-medium">{money(p.balance, true)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-[var(--color-ink-muted)]">Minimum</dt>
                  <dd className="tnum">{money(p.minimum, true)}</dd>
                </div>
                {p.paid > 0 && (
                  <div>
                    <dt className="text-xs text-[var(--color-ink-muted)]">Paid since</dt>
                    <dd className="tnum">{money(p.paid, true)}</dd>
                  </div>
                )}
              </dl>
              <Badge variant={badge.variant}>{badge.label(p)}</Badge>
            </div>
          )
        })}
      </CardContent>
      {seenUntil && (
        <CardFooter>
          <p className="text-xs text-[var(--color-ink-muted)]">
            Payments as far as your files show, up to {dateLabel(seenUntil)}.
          </p>
        </CardFooter>
      )}
    </Card>
  )
}

/**
 * Statements added before due dates were read carry none. Reading them only
 * fills in the due date; the transactions and their checks stay as they are.
 */
function ReadDueDates({
  unread,
  none,
  onRead,
}: {
  unread: number
  none: boolean
  onRead: () => void
}) {
  const [busy, setBusy] = React.useState(false)
  const [note, setNote] = React.useState<{ text: string; bad: boolean } | null>(null)

  async function read() {
    setBusy(true)
    setNote(null)
    try {
      const r = await api.readDueDates()
      const missing = r.checked - r.found - r.failed
      setNote({
        text:
          `Found a due date on ${r.found} of ${r.checked} statement${r.checked === 1 ? "" : "s"}.` +
          (missing > 0 ? ` ${missing} print none.` : "") +
          (r.failed > 0 ? ` ${r.failed} could not be opened.` : ""),
        bad: r.found === 0,
      })
      onRead()
    } catch (e) {
      setNote({ text: e instanceof Error ? e.message : String(e), bad: true })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3" data-write>
      <p className="min-w-[14rem] flex-1 text-sm text-[var(--color-ink-secondary)]">
        {none ? "See when each card is due. " : ""}
        {unread} statement{unread === 1 ? " was" : "s were"} added before due dates were
        read. Reading them only fills in the due date; nothing else changes.
        {note && (
          <span
            className={cn(
              "mt-1 block text-xs",
              note.bad ? "text-[var(--color-critical)]" : "text-[var(--color-ink-muted)]",
            )}
          >
            {note.text}
          </span>
        )}
      </p>
      <Button size="sm" variant="outline" onClick={read} disabled={busy}>
        {busy ? <Loader2 className="animate-spin" aria-hidden /> : <CalendarClock aria-hidden />}
        {busy ? "Reading" : "Read due dates"}
      </Button>
    </div>
  )
}
