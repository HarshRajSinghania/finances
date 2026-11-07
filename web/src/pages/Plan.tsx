/**
 * Budgets & goals — a limit per category, and what the surplus is for.
 *
 * Budgets are measured against the newest month the statements cover, not the
 * calendar month: statements arrive weeks after a month closes. A month still
 * in progress is projected to its end at the pace so far, so a limit warns
 * before it is broken rather than after.
 *
 * Goals are the planner's (`finances plan`), so the CLI and this page share
 * one plan file and can never disagree about when something lands.
 */

import { Check, Pencil, PiggyBank, Plus, Target, Trash2, X } from "lucide-react"
import * as React from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input, Progress } from "@/components/ui/misc"
import { EmptyState, ErrorState, LoadingRows, Spinner } from "@/components/ui/state"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import { money, monthLabel } from "@/lib/format"
import type { BudgetRow, BudgetStatus, Goal, Goals } from "@/lib/types"
import { cn } from "@/lib/utils"

export function Plan() {
  return (
    <div className="space-y-6">
      <p className="max-w-2xl text-sm leading-relaxed text-[var(--color-ink-secondary)]">
        A monthly limit for the categories you want to watch, and the goals your
        surplus is going towards. Both are saved next to your data, so{" "}
        <code className="font-mono text-xs">finances budget</code> and{" "}
        <code className="font-mono text-xs">finances plan</code> see the same thing.
      </p>
      <Budgets />
      <GoalsCard />
    </div>
  )
}

/* ================================================================ budgets */

const STATE_BAR: Record<BudgetRow["state"], string> = {
  over: "bg-[var(--color-critical)]",
  pace: "bg-[var(--color-warning)]",
  ok: "bg-[var(--color-good)]",
}

function Budgets() {
  const { data: fetched, error, loading, refetch } = useApi(() => api.budgets(), [])
  const [data, setData] = React.useState<BudgetStatus | null>(null)
  const [busy, setBusy] = React.useState<string | null>(null)
  const [writeError, setWriteError] = React.useState<string | null>(null)
  React.useEffect(() => setData(fetched), [fetched])

  const save = async (category: string, limit: number | null) => {
    setBusy(category)
    setWriteError(null)
    try {
      setData(await api.setBudget(category, limit))
    } catch (e) {
      setWriteError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <PiggyBank className="size-4" strokeWidth={1.75} aria-hidden />
          Budgets
          {data?.month && (
            <span className="font-normal text-[var(--color-ink-secondary)]">
              · {monthLabel(data.month)}
            </span>
          )}
        </CardTitle>
        {data?.month && (
          <CardDescription>
            {data.inProgress
              ? `Day ${data.daysElapsed} of ${data.daysInMonth} in the newest month on record; projections assume the same pace to month end.`
              : "The newest complete month on record."}
            {data.budgets.length > 0 &&
              ` ${money(data.totalSpent)} of ${money(data.totalLimit)} across your limits.`}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="space-y-5">
        {error && !data ? (
          <ErrorState error={error} onRetry={refetch} />
        ) : !data || (loading && !data) ? (
          <LoadingRows rows={4} className="p-0" />
        ) : !data.month ? (
          <EmptyState
            icon={PiggyBank}
            title="No spending yet"
            body="Budgets measure the newest month in your statements. Add some first."
          />
        ) : (
          <>
            {data.budgets.length === 0 ? (
              <p className="text-sm text-[var(--color-ink-secondary)]">
                No limits yet. Start from one of the suggestions below, or add your own.
              </p>
            ) : (
              <ul className="divide-y divide-[var(--color-hairline)]">
                {data.budgets.map((b) => (
                  <BudgetLine
                    key={b.category}
                    b={b}
                    inProgress={!!data.inProgress}
                    busy={busy === b.category}
                    onSave={(v) => save(b.category, v)}
                  />
                ))}
              </ul>
            )}

            {writeError && (
              <p className="text-xs text-[var(--color-critical)]">{writeError}</p>
            )}

            <AddBudget
              existing={data.budgets.map((b) => b.category)}
              busy={busy === "__new"}
              onAdd={async (cat, v) => {
                setBusy("__new")
                await save(cat, v)
              }}
            />

            {data.suggestions.length > 0 && (
              <div className="space-y-2">
                <p className="text-xs font-medium text-[var(--color-ink-secondary)]">
                  Suggested from your own averages
                </p>
                <div className="flex flex-wrap gap-2">
                  {data.suggestions.map((s) => (
                    <button
                      key={s.category}
                      type="button"
                      disabled={busy !== null}
                      onClick={() => save(s.category, s.suggested)}
                      title={`You average ${money(s.average)} a month`}
                      className="inline-flex items-center gap-1.5 rounded-full border border-[var(--color-hairline)] px-3 py-1 text-xs hover:bg-[var(--color-plane)] disabled:opacity-50"
                    >
                      <Plus className="size-3" aria-hidden />
                      {s.category}
                      <span className="tnum text-[var(--color-ink-muted)]">
                        {money(s.suggested)}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function BudgetLine({
  b,
  inProgress,
  busy,
  onSave,
}: {
  b: BudgetRow
  inProgress: boolean
  busy: boolean
  onSave: (limit: number | null) => void
}) {
  const [editing, setEditing] = React.useState(false)
  const [value, setValue] = React.useState(String(b.limit))

  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{b.category}</span>
        {b.state === "over" && <Badge variant="critical">Over</Badge>}
        {b.state === "pace" && <Badge variant="warning">On pace to go over</Badge>}

        {editing ? (
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              const v = Number(value)
              if (Number.isFinite(v) && v > 0) {
                onSave(v)
                setEditing(false)
              }
            }}
          >
            <Input
              type="number"
              min="1"
              step="any"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="h-8 w-24"
              aria-label={`Monthly limit for ${b.category}`}
              autoFocus
            />
            <Button size="icon" variant="ghost" type="submit" aria-label="Save" className="size-8">
              <Check />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              type="button"
              aria-label="Cancel"
              className="size-8"
              onClick={() => setEditing(false)}
            >
              <X />
            </Button>
          </form>
        ) : (
          <div className="flex items-center gap-1">
            <span className="tnum text-sm">
              {money(b.spent)}{" "}
              <span className="text-[var(--color-ink-muted)]">of {money(b.limit)}</span>
            </span>
            {busy ? (
              <Spinner className="ml-2" />
            ) : (
              <>
                <Button
                  size="icon"
                  variant="ghost"
                  className="size-8"
                  aria-label={`Edit ${b.category} limit`}
                  onClick={() => {
                    setValue(String(b.limit))
                    setEditing(true)
                  }}
                >
                  <Pencil />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  className="size-8"
                  aria-label={`Remove ${b.category} limit`}
                  onClick={() => onSave(null)}
                >
                  <Trash2 />
                </Button>
              </>
            )}
          </div>
        )}
      </div>
      <Progress value={Math.min(b.pct, 100)} indicatorClassName={STATE_BAR[b.state]} />
      <p className="tnum text-xs text-[var(--color-ink-muted)]">
        {b.left >= 0 ? `${money(b.left)} left` : `${money(-b.left)} over`}
        {inProgress && ` · projected ${money(b.projected)}`}
        {` · last month ${money(b.lastMonth)} · average ${money(b.average)}`}
      </p>
    </li>
  )
}

function AddBudget({
  existing,
  busy,
  onAdd,
}: {
  existing: string[]
  busy: boolean
  onAdd: (category: string, limit: number) => void | Promise<void>
}) {
  const [category, setCategory] = React.useState("")
  const [limit, setLimit] = React.useState("")
  const v = Number(limit)
  const ok = category.trim() !== "" && !existing.includes(category.trim()) && v > 0

  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!ok) return
        await onAdd(category.trim(), v)
        setCategory("")
        setLimit("")
      }}
    >
      <Input
        placeholder="Category, e.g. Dining"
        value={category}
        onChange={(e) => setCategory(e.target.value)}
        className="h-8 min-w-0 flex-1 basis-40"
        aria-label="Category"
      />
      <Input
        type="number"
        min="1"
        step="any"
        placeholder="Limit / month"
        value={limit}
        onChange={(e) => setLimit(e.target.value)}
        className="h-8 w-32"
        aria-label="Monthly limit"
      />
      <Button size="sm" variant="outline" type="submit" disabled={!ok || busy}>
        {busy ? <Spinner /> : <Plus />}
        Add limit
      </Button>
    </form>
  )
}

/* ================================================================== goals */

function GoalsCard() {
  const { data: fetched, error, loading, refetch } = useApi(() => api.goals(), [])
  const [data, setData] = React.useState<Goals | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [writeError, setWriteError] = React.useState<string | null>(null)
  React.useEffect(() => setData(fetched), [fetched])

  const run = async (fn: () => Promise<Goals>) => {
    setBusy(true)
    setWriteError(null)
    try {
      setData(await fn())
      return true
    } catch (e) {
      setWriteError(e instanceof Error ? e.message : String(e))
      return false
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Target className="size-4" strokeWidth={1.75} aria-hidden />
          Goals
        </CardTitle>
        {data && (
          <CardDescription>
            {data.surplus == null
              ? "No income on record yet, so there is no surplus to plan with. Set one with `finances plan sync`."
              : `${money(data.surplus)} spare each month, ${
                  data.surplusSource === "plan"
                    ? "as set in your plan"
                    : "worked out from income minus spending in your statements"
                }. Goals take it in priority order; when one lands its share moves to the next.`}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="space-y-5">
        {error && !data ? (
          <ErrorState error={error} onRetry={refetch} />
        ) : !data || (loading && !data) ? (
          <LoadingRows rows={2} className="p-0" />
        ) : (
          <>
            {data.goals.length === 0 ? (
              <p className="text-sm text-[var(--color-ink-secondary)]">
                No goals yet. Add one below — a trip, an emergency fund, a deposit.
              </p>
            ) : (
              <ul className="space-y-4">
                {data.goals.map((g) => (
                  <GoalLine
                    key={g.name}
                    g={g}
                    busy={busy}
                    onDelete={() => run(() => api.deleteGoal(g.name))}
                  />
                ))}
              </ul>
            )}
            {data.unallocated != null && data.goals.length > 0 && (
              <p className="tnum text-xs text-[var(--color-ink-muted)]">
                {money(data.unallocated)} a month not yet going to any goal.
              </p>
            )}
            {writeError && (
              <p className="text-xs text-[var(--color-critical)]">{writeError}</p>
            )}
            <AddGoal
              busy={busy}
              onAdd={(goal) => run(() => api.saveGoal(goal))}
            />
          </>
        )}
      </CardContent>
    </Card>
  )
}

function GoalLine({
  g,
  busy,
  onDelete,
}: {
  g: Goal
  busy: boolean
  onDelete: () => void
}) {
  return (
    <li className="space-y-2 rounded-lg border border-[var(--color-hairline)] p-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{g.name}</span>
        {g.late ? (
          <Badge variant="serious">Behind</Badge>
        ) : g.eta ? (
          <Badge variant="good">On track</Badge>
        ) : null}
        <Button
          size="icon"
          variant="ghost"
          className="size-8"
          aria-label={`Delete ${g.name}`}
          disabled={busy}
          onClick={onDelete}
        >
          <Trash2 />
        </Button>
      </div>
      <Progress value={g.pct} indicatorClassName="bg-[var(--color-series-1)]" />
      <p className="tnum text-xs text-[var(--color-ink-secondary)]">
        {money(g.saved)} of {money(g.target)}
        {g.by && ` · by ${monthLabel(g.by)}`}
        {g.allocated > 0 && ` · ${money(g.allocated)}/mo going to it`}
      </p>
      <p
        className={cn(
          "text-xs",
          g.late ? "text-[var(--color-critical)]" : "text-[var(--color-ink-muted)]",
        )}
      >
        {g.eta
          ? `Lands ${monthLabel(g.eta)}${g.late && g.by ? `, after the ${monthLabel(g.by)} deadline` : ""}.`
          : g.remaining <= 0
            ? "Done."
            : "At the current surplus this never lands."}
        {g.required != null && g.required > g.allocated &&
          ` Needs ${money(g.required)}/mo to make the deadline.`}
      </p>
    </li>
  )
}

function AddGoal({
  busy,
  onAdd,
}: {
  busy: boolean
  onAdd: (g: { name: string; target: number; saved?: number; by?: string | null }) => Promise<boolean>
}) {
  const [name, setName] = React.useState("")
  const [target, setTarget] = React.useState("")
  const [saved, setSaved] = React.useState("")
  const [by, setBy] = React.useState("")
  const t = Number(target)
  const ok = name.trim() !== "" && t > 0

  return (
    <form
      className="grid gap-2 sm:grid-cols-[1fr_8rem_8rem_9rem_auto]"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!ok) return
        const done = await onAdd({
          name: name.trim(),
          target: t,
          saved: saved ? Number(saved) : undefined,
          by: by || null,
        })
        if (done) {
          setName("")
          setTarget("")
          setSaved("")
          setBy("")
        }
      }}
    >
      <Input
        placeholder="Goal, e.g. Japan trip"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="h-8"
        aria-label="Goal name"
      />
      <Input
        type="number"
        min="1"
        step="any"
        placeholder="Target"
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        className="h-8"
        aria-label="Target amount"
      />
      <Input
        type="number"
        min="0"
        step="any"
        placeholder="Saved so far"
        value={saved}
        onChange={(e) => setSaved(e.target.value)}
        className="h-8"
        aria-label="Already saved"
      />
      <Input
        type="month"
        value={by}
        onChange={(e) => setBy(e.target.value)}
        className="h-8"
        aria-label="Deadline month"
      />
      <Button size="sm" variant="outline" type="submit" disabled={!ok || busy}>
        {busy ? <Spinner /> : <Plus />}
        Add goal
      </Button>
    </form>
  )
}
