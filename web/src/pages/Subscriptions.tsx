/**
 * Subscriptions — the charges that repeat, and what a year of them costs.
 *
 * The annual figure leads, because it is the number people react to: $25 a
 * month reads as a rounding error, $300 a year reads as a decision.
 *
 * Three things are lifted out of the table and named, because they are the
 * findings rather than the data — a charge that stopped arriving, a price that
 * moved, and a row the detector is not sure about. The last one is marked in
 * neutral ink on purpose: low confidence is a statement about the evidence, not
 * a state of the subscription, and the status palette is reserved for states.
 */

import {
  AlertTriangle,
  Ban,
  Check,
  ChevronDown,
  CircleHelp,
  Scissors,
  Percent,
  Repeat,
  TrendingDown,
  TrendingUp,
  Undo2,
} from "lucide-react"
import * as React from "react"

import { Stat } from "@/components/Stat"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { EmptyState, ErrorState, LoadingRows, Spinner } from "@/components/ui/state"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableWrap,
} from "@/components/ui/table"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import {
  cadenceLabel,
  dateLabel,
  daysAgo,
  money,
  percent,
  relativeDay,
  signedMoney,
  signedPercent,
} from "@/lib/format"
import type {
  Cadence,
  Recurring,
  ReviewItem,
  SubscriptionMark,
  SubscriptionReview,
} from "@/lib/types"
import { cn } from "@/lib/utils"

/** How many charges a year each cadence produces. Matches the API's perYear. */
const PER_YEAR: Record<Cadence, number> = {
  weekly: 52,
  biweekly: 26,
  monthly: 12,
  quarterly: 4,
  semiannual: 2,
  annual: 1,
}

/** Below this, the gap between charges or the amount wobbles enough to doubt the row. */
const UNCERTAIN = 0.65

/** The CLI flags drift past 4%; matching it keeps both tools telling one story. */
const DRIFT_PCT = 4

/** api.py files interest under this category and leaves it out unless asked. */
const INTEREST_CATEGORY = "Fees/Interest"

const isInterest = (r: Recurring) => r.category === INTEREST_CATEGORY

const sum = (rows: Recurring[], pick: (r: Recurring) => number) =>
  rows.reduce((total, r) => total + pick(r), 0)

/** What the price change costs over a year on its own, at the new price. */
const driftPerYear = (r: Recurring) => r.priceChange * (PER_YEAR[r.cadence] ?? 12)

export function Subscriptions() {
  const [includeInterest, setIncludeInterest] = React.useState(false)
  const { data, error, loading, refetch } = useApi(
    () => api.recurring(includeInterest),
    [includeInterest],
  )

  const rows = React.useMemo(
    () => [...(data?.recurring ?? [])].sort((a, b) => b.perYear - a.perYear),
    [data],
  )

  const active = rows.filter((r) => !r.isStale)
  const stale = rows.filter((r) => r.isStale)
  const drifting = rows
    .filter((r) => r.priceChange !== 0 && Math.abs(r.pricePct) >= DRIFT_PCT)
    .sort((a, b) => b.pricePct - a.pricePct)
  const uncertain = rows.filter((r) => r.confidence < UNCERTAIN)
  const interest = rows.filter(isInterest)

  const perMonth = sum(active, (r) => r.perMonth)
  const perYear = sum(active, (r) => r.perYear)
  const staleYear = sum(stale, (r) => r.perYear)

  return (
    <div className="space-y-6">
      <p className="max-w-2xl text-sm leading-relaxed text-[var(--color-ink-secondary)]">
        Charges that repeat on a schedule, found by looking for the same merchant at
        the same interval for a similar amount. None of this is read from a list of
        subscriptions — it is a pattern in the transactions, so every row carries how
        steady that pattern actually is.
      </p>

      <CancelList />

      <InterestToggle
        on={includeInterest}
        busy={loading}
        interest={interest}
        onToggle={() => setIncludeInterest((v) => !v)}
      />

      {error && !data ? (
        <div className="card">
          <ErrorState error={error} onRetry={refetch} />
        </div>
      ) : loading && !data ? (
        <LoadingView />
      ) : rows.length === 0 ? (
        <div className="card">
          <EmptyState
            icon={Repeat}
            title="No repeating charges found"
            body="Nothing in these statements bills on a steady schedule. A charge has to appear at least three times at a regular interval before it is counted."
          />
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Stat
              label="Per year"
              emphasis
              value={money(perYear)}
              sub={`${active.length} ${active.length === 1 ? "charge" : "charges"} still arriving`}
              hint="Every charge below at its current price, carried out to twelve months. Charges that stopped arriving are left out of this figure."
            />
            <Stat
              label="Per month"
              value={money(perMonth)}
              sub="promised before anything else is spent"
              hint="A yearly or quarterly charge is spread across the months it covers, so this is not simply the sum of this month's bills."
            />
            <Stat
              label="Overdue, per year"
              value={money(staleYear)}
              sub={
                stale.length === 0
                  ? "every charge arrived when expected"
                  : `${stale.length} ${stale.length === 1 ? "charge was" : "charges were"} due and never arrived`
              }
              hint="Worth what these charges cost per year while they were still arriving. Not counted in the totals beside it."
            />
          </div>

          {stale.length > 0 && <StaleCard rows={stale} total={staleYear} />}
          {drifting.length > 0 && <DriftCard rows={drifting} />}
          {uncertain.length > 0 && <UncertainCard rows={uncertain} />}

          <ChargeTable rows={rows} committedMonthly={data?.committedMonthly ?? null} />
        </>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ toggle */

/**
 * Interest is excluded by default in the backend, and the reason is worth
 * saying out loud rather than hiding behind a switch: it bills exactly like a
 * subscription and is nothing like one.
 */
function InterestToggle({
  on,
  busy,
  interest,
  onToggle,
}: {
  on: boolean
  busy: boolean
  interest: Recurring[]
  onToggle: () => void
}) {
  const interestYear = sum(interest, (r) => r.perYear)

  return (
    <div className="card flex flex-wrap items-start justify-between gap-x-4 gap-y-3 p-4">
      <div className="min-w-0 flex-1 basis-64">
        <p className="text-sm font-medium">
          Interest is {on ? "included" : "excluded"}
        </p>
        <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          Interest lands on the card every month at a steady amount, so a cadence
          detector cannot tell it apart from a subscription. It is a consequence of a
          balance rather than something you can cancel, so it is left out by default —
          counting it as a commitment makes the part you can actually change look
          smaller than it is.
        </p>
        {on && interest.length > 0 && (
          <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-[var(--color-ink-muted)]">
            Adds {money(interestYear)} a year across{" "}
            {interest.length === 1 ? "one charge" : `${interest.length} charges`}:{" "}
            {interest.map((r) => r.name).join(", ")}. Those rows are marked below.
          </p>
        )}
      </div>

      <Button
        variant="outline"
        size="sm"
        aria-pressed={on}
        disabled={busy}
        onClick={onToggle}
      >
        {busy && <Spinner />}
        {on ? "Exclude interest" : "Include interest"}
      </Button>
    </div>
  )
}

/* ---------------------------------------------------------------- findings */

function StaleCard({ rows, total }: { rows: Recurring[]; total: number }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <AlertTriangle
            className="size-4 shrink-0 text-[var(--color-warning)]"
            aria-hidden
            strokeWidth={1.75}
          />
          {rows.length === 1
            ? "One charge stopped arriving"
            : `${rows.length} charges stopped arriving`}
        </CardTitle>
        <CardDescription className="max-w-2xl leading-relaxed">
          The next charge was due and never landed. Either it was cancelled and
          forgotten, or the card on file expired — in which case it comes back the day
          the card is replaced. Both are worth checking, and they need different
          answers.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-[var(--color-hairline)]">
          {rows.map((r) => {
            const late = daysAgo(r.nextExpected)
            return (
              <li
                key={r.key}
                className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2.5 first:pt-0 last:pb-0"
              >
                <div className="min-w-0 flex-1 basis-48">
                  <p className="truncate text-sm font-medium">{r.name}</p>
                  <p className="mt-0.5 text-xs text-[var(--color-ink-muted)]">
                    <span className="tnum">{money(r.amount, true)}</span>{" "}
                    {cadenceLabel[r.cadence].toLowerCase()} · last seen{" "}
                    <span className="tnum">{dateLabel(r.lastSeen)}</span> · due{" "}
                    <span className="tnum">{dateLabel(r.nextExpected)}</span>
                    {late !== null && late > 0 && `, ${late} days ago`}
                  </p>
                </div>
                <p className="tnum shrink-0 text-sm font-medium">
                  {money(r.perYear)}
                  <span className="text-xs font-normal text-[var(--color-ink-muted)]">
                    {" "}
                    a year
                  </span>
                </p>
              </li>
            )
          })}
        </ul>
      </CardContent>
      <CardFooter>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          <span className="tnum">{money(total)}</span> a year in charges that may or
          may not still be live.
        </p>
      </CardFooter>
    </Card>
  )
}

function DriftCard({ rows }: { rows: Recurring[] }) {
  const net = rows.reduce((t, r) => t + driftPerYear(r), 0)
  const risen = rows.filter((r) => r.priceChange > 0)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Percent
            className="size-4 shrink-0 text-[var(--color-ink-muted)]"
            aria-hidden
            strokeWidth={1.75}
          />
          {rows.length === 1 ? "One price has moved" : `${rows.length} prices have moved`}
        </CardTitle>
        <CardDescription className="max-w-2xl leading-relaxed">
          What is charged now is not what was charged when the run started. The yearly
          figure is the effect of the change on its own, if the new price holds — the
          rest of the cost was already there.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-[var(--color-hairline)]">
          {rows.map((r) => {
            const Icon = r.priceChange > 0 ? TrendingUp : TrendingDown
            return (
              <li
                key={r.key}
                className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2.5 first:pt-0 last:pb-0"
              >
                <div className="min-w-0 flex-1 basis-48">
                  <p className="truncate text-sm font-medium">{r.name}</p>
                  <p className="mt-0.5 text-xs text-[var(--color-ink-muted)]">
                    was <span className="tnum">{money(r.amount - r.priceChange, true)}</span>
                    , now <span className="tnum">{money(r.amount, true)}</span> —{" "}
                    <span className="tnum">{signedMoney(r.priceChange, true)}</span> a
                    charge, {cadenceLabel[r.cadence].toLowerCase()}
                  </p>
                </div>
                <div className="flex shrink-0 items-baseline gap-3">
                  <Badge variant="outline">
                    <Icon aria-hidden />
                    <span className="tnum">{signedPercent(r.pricePct)}</span>
                  </Badge>
                  <p className="tnum text-sm font-medium">
                    {signedMoney(driftPerYear(r))}
                    <span className="text-xs font-normal text-[var(--color-ink-muted)]">
                      {" "}
                      a year
                    </span>
                  </p>
                </div>
              </li>
            )
          })}
        </ul>
      </CardContent>
      <CardFooter>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          {risen.length > 0 && (
            <>
              Rises alone add{" "}
              <span className="tnum">
                {signedMoney(risen.reduce((t, r) => t + driftPerYear(r), 0))}
              </span>{" "}
              a year.{" "}
            </>
          )}
          Every change together:{" "}
          <span className="tnum">{signedMoney(net)}</span> a year.
        </p>
      </CardFooter>
    </Card>
  )
}

function UncertainCard({ rows }: { rows: Recurring[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CircleHelp
            className="size-4 shrink-0 text-[var(--color-ink-muted)]"
            aria-hidden
            strokeWidth={1.75}
          />
          {rows.length === 1
            ? "One of these may not be a subscription"
            : `${rows.length} of these may not be subscriptions`}
        </CardTitle>
        <CardDescription className="max-w-2xl leading-relaxed">
          The gap between charges, or the amount, moves around enough that this could
          be a coincidence rather than a standing charge. Shown anyway — dropping it
          would be a silent decision — but read the yearly figure as an estimate.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-[var(--color-hairline)]">
          {rows.map((r) => (
            <li
              key={r.key}
              className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2.5 first:pt-0 last:pb-0"
            >
              <div className="min-w-0 flex-1 basis-48">
                <p className="truncate text-sm font-medium">{r.name}</p>
                <p className="mt-0.5 text-xs text-[var(--color-ink-muted)]">
                  {cadenceLabel[r.cadence].toLowerCase()}, seen {r.n} times ·{" "}
                  <span className="tnum">{money(r.amount, true)}</span> a charge
                </p>
              </div>
              <div className="flex shrink-0 items-baseline gap-3">
                <Badge variant="outline">
                  <span className="tnum">{percent(r.confidence * 100)}</span> steady
                </Badge>
                <p className="tnum text-sm font-medium">
                  {money(r.perYear)}
                  <span className="text-xs font-normal text-[var(--color-ink-muted)]">
                    {" "}
                    a year
                  </span>
                </p>
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------------- table */

function ChargeTable({
  rows,
  committedMonthly,
}: {
  rows: Recurring[]
  committedMonthly: number | null
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Every repeating charge</CardTitle>
        <CardDescription>
          Sorted by what each one costs a year — the order in which they are worth
          arguing about.
        </CardDescription>
      </CardHeader>

      <TableWrap className="border-t border-[var(--color-hairline)]">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="min-w-[13rem]">Charge</TableHead>
              <TableHead>Every</TableHead>
              <TableHead className="text-right">Amount</TableHead>
              <TableHead className="text-right">Per year</TableHead>
              <TableHead className="text-right">Seen</TableHead>
              <TableHead>Next due</TableHead>
              <TableHead className="text-right">Steady</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => {
              const late = daysAgo(r.nextExpected)
              const drifted = r.priceChange !== 0 && Math.abs(r.pricePct) >= DRIFT_PCT
              const Icon = r.priceChange > 0 ? TrendingUp : TrendingDown
              return (
                <TableRow key={r.key}>
                  <TableCell>
                    <p className="font-medium">{r.name}</p>
                    <p className="mt-0.5 text-xs text-[var(--color-ink-muted)]">
                      {r.category}
                    </p>
                    <div className="mt-1.5 flex flex-wrap gap-1.5 empty:mt-0">
                      {r.isStale && (
                        <Badge variant="warning">
                          <AlertTriangle aria-hidden />
                          Overdue
                        </Badge>
                      )}
                      {drifted && (
                        <Badge variant="outline">
                          <Icon aria-hidden />
                          <span className="tnum">{signedPercent(r.pricePct)}</span>
                        </Badge>
                      )}
                      {r.confidence < UNCERTAIN && (
                        <Badge variant="outline">
                          <CircleHelp aria-hidden />
                          Less certain
                        </Badge>
                      )}
                      {isInterest(r) && (
                        <Badge variant="outline">Interest, not a subscription</Badge>
                      )}
                    </div>
                  </TableCell>

                  <TableCell className="whitespace-nowrap text-[var(--color-ink-secondary)]">
                    {cadenceLabel[r.cadence] ?? r.cadence}
                  </TableCell>

                  <TableCell className="tnum text-right whitespace-nowrap">
                    {money(r.amount, true)}
                  </TableCell>

                  <TableCell className="tnum text-right font-medium whitespace-nowrap">
                    {money(r.perYear)}
                  </TableCell>

                  <TableCell className="tnum text-right">{r.n}</TableCell>

                  <TableCell className="whitespace-nowrap">
                    {r.nextExpected ? (
                      <>
                        <span className="tnum">{dateLabel(r.nextExpected)}</span>
                        <span className="block text-xs text-[var(--color-ink-muted)]">
                          {r.isStale && late !== null && late > 0
                            ? `${late} days overdue`
                            : relativeDay(r.nextExpected)}
                        </span>
                      </>
                    ) : (
                      <span className="text-[var(--color-ink-muted)]">—</span>
                    )}
                  </TableCell>

                  <TableCell
                    className={cn(
                      "tnum text-right whitespace-nowrap",
                      r.confidence < UNCERTAIN && "text-[var(--color-ink-muted)]",
                    )}
                  >
                    {percent(r.confidence * 100)}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </TableWrap>

      <CardFooter className="flex-wrap gap-1">
        <p className="text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          &ldquo;Steady&rdquo; is how regular the interval and the amount have been, not
          how sure we are that you signed up for it.
          {committedMonthly !== null && (
            <>
              {" "}
              The rest of the app counts{" "}
              <span className="tnum">{money(committedMonthly)}</span> a month as
              committed: every detected subscription including the overdue ones, and
              never interest.
            </>
          )}
        </p>
      </CardFooter>
    </Card>
  )
}

/* ----------------------------------------------------------------- loading */

function LoadingView() {
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Stat label="Per year" value={null} loading emphasis />
        <Stat label="Per month" value={null} loading />
        <Stat label="Overdue, per year" value={null} loading />
      </div>
      <div className="card">
        <LoadingRows rows={8} />
      </div>
    </>
  )
}

/* ------------------------------------------------------------- cancel list */

/**
 * The findings turned into decisions. Each candidate says why it is here, and
 * the two buttons record what you did about it: kept stops it being suggested,
 * cancelled arms a check that flags any charge after that day — a cancellation
 * that did not take is the most expensive kind.
 */
function CancelList() {
  const { data: fetched, error, refetch } = useApi(() => api.subscriptionReview(), [])
  const [data, setData] = React.useState<SubscriptionReview | null>(null)
  const [busy, setBusy] = React.useState<string | null>(null)
  const [writeError, setWriteError] = React.useState<string | null>(null)
  const [showDecided, setShowDecided] = React.useState(false)
  React.useEffect(() => setData(fetched), [fetched])

  const mark = async (key: string, status: SubscriptionMark) => {
    setBusy(key)
    setWriteError(null)
    try {
      setData(await api.markSubscription(key, status))
    } catch (e) {
      setWriteError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  if (error && !data) {
    return (
      <div className="card">
        <ErrorState error={error} onRetry={refetch} />
      </div>
    )
  }
  if (!data) return null

  const decided = [...data.kept, ...data.cancelled]
  if (data.candidates.length === 0 && data.stillCharging.length === 0 && decided.length === 0) {
    return null
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Scissors className="size-4" strokeWidth={1.75} aria-hidden />
          Cancel these?
        </CardTitle>
        <CardDescription>
          {data.candidates.length > 0 ? (
            <>
              <span className="tnum font-medium text-[var(--color-ink)]">
                {money(data.savingsPerYear)} a year
              </span>{" "}
              if every one of these went. Rent, insurance and utilities are left off —
              nobody cancels those to save money.
            </>
          ) : (
            "Nothing left to decide on."
          )}
          {data.savedPerYear > 0 &&
            ` Already cancelled: ${money(data.savedPerYear)} a year saved.`}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.stillCharging.map((r) => (
          <CancelRow
            key={r.key}
            r={r}
            alert
            busy={busy === r.key}
            actions={
              <Button size="sm" variant="outline" onClick={() => mark(r.key, null)}>
                <Undo2 /> Clear
              </Button>
            }
          />
        ))}

        {data.candidates.map((r) => (
          <CancelRow
            key={r.key}
            r={r}
            busy={busy === r.key}
            actions={
              <>
                <Button size="sm" variant="outline" onClick={() => mark(r.key, "keep")}>
                  <Check /> Keep
                </Button>
                <Button size="sm" variant="danger" onClick={() => mark(r.key, "cancelled")}>
                  <Ban /> Cancelled
                </Button>
              </>
            }
          />
        ))}

        {writeError && <p className="text-xs text-[var(--color-critical)]">{writeError}</p>}

        {decided.length > 0 && (
          <div className="border-t border-[var(--color-hairline)] pt-3">
            <button
              type="button"
              className="flex items-center gap-1.5 text-xs text-[var(--color-ink-secondary)] hover:text-[var(--color-ink)]"
              aria-expanded={showDecided}
              onClick={() => setShowDecided((v) => !v)}
            >
              <ChevronDown
                className={cn("size-3.5 transition-transform", showDecided && "rotate-180")}
                aria-hidden
              />
              {data.kept.length} kept · {data.cancelled.length} cancelled
            </button>
            {showDecided && (
              <ul className="mt-2 divide-y divide-[var(--color-hairline)]">
                {decided.map((r) => {
                  const cancelled = data.cancelled.includes(r)
                  return (
                    <li key={r.key} className="flex items-center gap-3 py-2 text-sm">
                      <span className="min-w-0 flex-1 truncate">{r.name}</span>
                      <Badge variant={cancelled ? "good" : "neutral"}>
                        {cancelled ? "Cancelled" : "Kept"}
                        {r.markedAt ? ` ${dateLabel(r.markedAt)}` : ""}
                      </Badge>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy === r.key}
                        onClick={() => mark(r.key, null)}
                      >
                        {busy === r.key ? <Spinner /> : <Undo2 />}
                        Undo
                      </Button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function CancelRow({
  r,
  alert = false,
  busy,
  actions,
}: {
  r: ReviewItem
  alert?: boolean
  busy: boolean
  actions: React.ReactNode
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-lg border p-3.5 sm:flex-row sm:items-center",
        alert
          ? "border-[var(--color-critical)]/40 bg-[var(--color-critical)]/5"
          : "border-[var(--color-hairline)]",
      )}
    >
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          {alert && (
            <AlertTriangle
              className="size-3.5 self-center text-[var(--color-critical)]"
              aria-hidden
            />
          )}
          <span className="truncate text-sm font-medium">{r.name}</span>
          <span className="tnum text-xs text-[var(--color-ink-secondary)]">
            {money(r.perMonth, true)}/mo · {cadenceLabel[r.cadence] ?? r.cadence}
          </span>
        </div>
        <ul className="space-y-0.5">
          {r.reasons.map((reason) => (
            <li
              key={reason}
              className={cn(
                "text-xs",
                alert && reason === r.reasons[0]
                  ? "font-medium text-[var(--color-critical)]"
                  : "text-[var(--color-ink-muted)]",
              )}
            >
              {reason}
            </li>
          ))}
        </ul>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {busy ? <Spinner /> : actions}
      </div>
    </div>
  )
}
