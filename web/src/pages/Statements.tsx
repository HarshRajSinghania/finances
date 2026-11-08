import {
  CalendarOff,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleHelp,
  FileText,
  Trash2,
} from "lucide-react"
import * as React from "react"

import { StatementSources } from "@/components/StatementSources"
import { UploadDropzone } from "@/components/UploadDropzone"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Hint } from "@/components/ui/misc"
import { ErrorState, LoadingRows, Spinner } from "@/components/ui/state"
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
import { dateLabel, dateShort, money, percent } from "@/lib/format"
import type { CoverageGap, Statement, Summary } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * The statements the store was built from — and the way to add more.
 *
 * Everything else in this app is derived; this page is the evidence. Each file
 * carries its own verdict: whether the transactions extracted from it add up to
 * the total the statement itself prints. Thirty-odd files are only legible
 * grouped by the account they belong to, so that is the primary structure, with
 * the balance chain and any missing months sitting beside the account they are
 * about rather than in a separate list of complaints.
 */

interface Group {
  key: string
  issuer: string
  last4: string
  /** Oldest first. */
  statements: Statement[]
  gaps: CoverageGap[]
}

/** Sortable key for a statement's period, however much of it was printed. */
function periodKey(s: Statement): string {
  return s.periodEnd ?? s.periodStart ?? s.period ?? ""
}

function periodLabel(s: Statement): string {
  if (s.periodStart && s.periodEnd) {
    return `${dateShort(s.periodStart)} – ${dateLabel(s.periodEnd)}`
  }
  if (s.periodEnd) return dateLabel(s.periodEnd)
  return s.period || "Period not printed"
}

export function Statements({
  summary,
  onChanged,
}: {
  summary: Summary | null
  onChanged: () => void
}) {
  const { data, error, loading, refetch } = useApi(() => api.statements(), [])
  const sources = useApi(() => api.sources(), [])

  // Uploads and deletes change the reconciliation verdict in the header, so the
  // app-level summary is reloaded alongside this page's own data.
  const refetchSources = sources.refetch
  const changed = React.useCallback(() => {
    refetch()
    refetchSources()
    onChanged()
  }, [refetch, refetchSources, onChanged])

  const groups = React.useMemo<Group[]>(() => {
    if (!data) return []
    const by = new Map<string, Group>()
    for (const s of data.statements) {
      const key = `${s.issuer} ${s.accountLast4}`.trim()
      let g = by.get(key)
      if (!g) {
        g = { key, issuer: s.issuer, last4: s.accountLast4, statements: [], gaps: [] }
        by.set(key, g)
      }
      g.statements.push(s)
    }
    for (const g of by.values()) {
      g.statements.sort((a, b) => periodKey(a).localeCompare(periodKey(b)))
      // A gap is a hole in one account's record, so it belongs to that account.
      g.gaps = data.coverage.gaps.filter((gap) => gap.account === g.key)
    }
    return [...by.values()].sort((a, b) => a.key.localeCompare(b.key))
  }, [data])

  if (error) {
    return (
      <Card>
        <ErrorState error={error} onRetry={refetch} />
      </Card>
    )
  }

  if (loading && !data) {
    return (
      <div className="space-y-4">
        <Card>
          <LoadingRows rows={2} />
        </Card>
        <Card>
          <LoadingRows rows={8} />
        </Card>
      </div>
    )
  }

  if (!data) return null

  // Nothing in the store: the only useful thing on this page is the dropzone,
  // so it becomes the page.
  if (data.statements.length === 0) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <div className="space-y-1">
          <h2 className="text-sm font-semibold">No statements yet</h2>
          <p className="text-xs leading-relaxed text-[var(--color-ink-secondary)]">
            This dashboard reads nothing but the statements you give it. Add the first
            one and every figure in the app becomes traceable back to a line on a real
            statement.
          </p>
        </div>
        <UploadDropzone variant="full" onUploaded={changed} />
      </div>
    )
  }

  const cov = data.coverage
  const attributed = data.statements.reduce((n, s) => n + s.transactionCount, 0)
  const unattributed = summary ? summary.transactions - attributed : 0

  return (
    <div className="space-y-5">
      <Verdict
        coverage={cov}
        accounts={groups.length}
        attributed={attributed}
        unattributed={unattributed}
      />

      <UploadDropzone onUploaded={changed} />

      {sources.data && <StatementSources data={sources.data} onChange={changed} />}

      <div className="space-y-4">
        {groups.map((g) => (
          <AccountCard key={g.key} group={g} onChanged={changed} />
        ))}
      </div>
    </div>
  )
}

/* ----------------------------------------------------------------- verdict */

function Verdict({
  coverage,
  accounts,
  attributed,
  unattributed,
}: {
  coverage: Summary["coverage"]
  accounts: number
  attributed: number
  unattributed: number
}) {
  const { statements, reconciled, mismatched, unchecked, pct, gaps } = coverage
  const clean = mismatched === 0 && unchecked === 0

  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        {clean ? (
          <CircleCheck
            className="mt-0.5 size-5 shrink-0 text-[var(--color-good)]"
            aria-hidden
          />
        ) : mismatched > 0 ? (
          <CircleAlert
            className="mt-0.5 size-5 shrink-0 text-[var(--color-critical)]"
            aria-hidden
          />
        ) : (
          <CircleHelp
            className="mt-0.5 size-5 shrink-0 text-[var(--color-warning)]"
            aria-hidden
          />
        )}

        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-sm font-medium">
            {reconciled} of {statements} statements reconcile against their own printed
            totals
            {statements > 0 && (
              <span className="tnum font-normal text-[var(--color-ink-muted)]">
                {" "}
                ({percent(pct, pct % 1 === 0 ? 0 : 1)})
              </span>
            )}
            .
          </p>
          <p className="text-xs leading-relaxed text-[var(--color-ink-secondary)]">
            {attributed.toLocaleString()} transactions across {accounts} account
            {accounts === 1 ? "" : "s"}, every one of them read from a file listed below.
            {mismatched > 0 &&
              ` ${mismatched} statement${mismatched === 1 ? "" : "s"} disagree with the totals printed on them; figures drawn from those files are not verified.`}
            {unchecked > 0 &&
              ` ${unchecked} printed no summary total, so there was nothing to check against — unverified, not wrong.`}
            {gaps.length > 0 &&
              ` ${gaps.length} gap${gaps.length === 1 ? "" : "s"} in the record: those months have no statement at all, so totals over them are incomplete.`}
            {unattributed > 0 &&
              ` ${unattributed.toLocaleString()} transactions in the store come from no statement listed here.`}
          </p>
        </div>
      </div>
    </Card>
  )
}

/* ----------------------------------------------------------------- account */

function AccountCard({ group, onChanged }: { group: Group; onChanged: () => void }) {
  const [open, setOpen] = React.useState<Set<string>>(() => new Set())
  const [confirming, setConfirming] = React.useState<string | null>(null)

  const rows = React.useMemo(
    () => [...group.statements].reverse(), // newest first
    [group.statements],
  )

  const mismatched = group.statements.filter((s) => s.status === "mismatch").length
  const unchecked = group.statements.filter((s) => s.status === "unchecked").length
  const n = group.statements.length

  const first = group.statements[0]
  const last = group.statements[n - 1]
  const opening = group.statements.find((s) => s.previousBalance !== null)
  const closing = [...group.statements].reverse().find((s) => s.newBalance !== null)

  // A statement's opening balance should be the previous statement's closing
  // balance. Where it is not, the chain has a break and the account's history
  // is not continuous, whatever the individual files say.
  const breaks = group.statements.filter((s, i) => {
    if (i === 0) return false
    const prev = group.statements[i - 1]
    if (prev.newBalance === null || s.previousBalance === null) return false
    return Math.abs(prev.newBalance - s.previousBalance) > 0.01
  })

  function toggle(file: string) {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(file)) next.delete(file)
      else next.add(file)
      return next
    })
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 p-4 sm:p-5">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold tracking-tight">
            {group.issuer}{" "}
            <span className="tnum font-normal text-[var(--color-ink-secondary)]">
              ····{group.last4}
            </span>
          </h2>
          <p className="mt-0.5 text-xs text-[var(--color-ink-secondary)]">
            {n} statement{n === 1 ? "" : "s"}
            {first.periodStart && last.periodEnd && (
              <>
                {" · "}
                {dateLabel(first.periodStart)} – {dateLabel(last.periodEnd)}
              </>
            )}
          </p>
        </div>

        {mismatched > 0 ? (
          <Badge variant="critical">
            <CircleAlert aria-hidden />
            {mismatched} do not add up
          </Badge>
        ) : unchecked > 0 ? (
          <Badge variant="warning">
            <CircleHelp aria-hidden />
            {unchecked} unchecked
          </Badge>
        ) : (
          <Badge variant="good">
            <CircleCheck aria-hidden />
            {n} of {n} reconcile
          </Badge>
        )}
      </div>

      {(opening?.previousBalance != null || closing?.newBalance != null) && (
        <p className="px-4 pb-3 text-xs text-[var(--color-ink-secondary)] sm:px-5">
          Balance{" "}
          <span className="tnum">{money(opening?.previousBalance ?? null, true)}</span>
          {opening?.periodStart && (
            <span className="text-[var(--color-ink-muted)]">
              {" "}
              ({dateLabel(opening.periodStart)})
            </span>
          )}{" "}
          →{" "}
          <span className="tnum">{money(closing?.newBalance ?? null, true)}</span>
          {closing?.periodEnd && (
            <span className="text-[var(--color-ink-muted)]">
              {" "}
              ({dateLabel(closing.periodEnd)})
            </span>
          )}
          {breaks.length === 0 ? (
            <span className="text-[var(--color-ink-muted)]">
              {" "}
              · each statement opens where the last one closed
            </span>
          ) : (
            <span className="text-[var(--color-ink)]">
              {" · "}
              {breaks.length} break{breaks.length === 1 ? "" : "s"} in the chain:{" "}
              {breaks.map((b) => b.sourceFile).join(", ")} open
              {breaks.length === 1 ? "s" : ""} on a different balance than the previous
              statement closed on.
            </span>
          )}
        </p>
      )}

      {group.gaps.map((gap) => (
        <div
          key={`${gap.after}-${gap.before}`}
          className="mx-4 mb-3 flex items-start gap-2 rounded-md border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 px-3 py-2 sm:mx-5"
        >
          <CalendarOff
            className="mt-0.5 size-3.5 shrink-0 text-[var(--color-ink)]"
            aria-hidden
          />
          <p className="text-xs leading-relaxed">
            No statement between {dateLabel(gap.after)} and {dateLabel(gap.before)} —{" "}
            <span className="tnum">{gap.days}</span> days of this account&rsquo;s history
            are missing, so any total spanning them is incomplete.
          </p>
        </div>
      ))}

      <TableWrap className="border-t border-[var(--color-hairline)]">
        <Table>
          <caption className="sr-only">
            Statements for {group.issuer} account ending {group.last4}, newest first
          </caption>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-8">
                <span className="sr-only">Expand</span>
              </TableHead>
              <TableHead>Period</TableHead>
              <TableHead>File</TableHead>
              <TableHead className="text-right">Transactions</TableHead>
              <TableHead className="text-right">Charges</TableHead>
              <TableHead>Reconciliation</TableHead>
              <TableHead className="w-10">
                <span className="sr-only">Remove</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((s) => (
              <StatementRow
                key={s.sourceFile}
                statement={s}
                open={open.has(s.sourceFile)}
                confirming={confirming === s.sourceFile}
                onToggle={() => toggle(s.sourceFile)}
                onAskDelete={() => {
                  setOpen((prev) => new Set(prev).add(s.sourceFile))
                  setConfirming(s.sourceFile)
                }}
                onCancelDelete={() => setConfirming(null)}
                onDeleted={() => {
                  setConfirming(null)
                  onChanged()
                }}
              />
            ))}
          </TableBody>
        </Table>
      </TableWrap>
    </Card>
  )
}

/* --------------------------------------------------------------------- row */

function StatementRow({
  statement: s,
  open,
  confirming,
  onToggle,
  onAskDelete,
  onCancelDelete,
  onDeleted,
}: {
  statement: Statement
  open: boolean
  confirming: boolean
  onToggle: () => void
  onAskDelete: () => void
  onCancelDelete: () => void
  onDeleted: () => void
}) {
  const [deleting, setDeleting] = React.useState(false)
  const [deleteError, setDeleteError] = React.useState<string | null>(null)
  const panelId = `detail-${s.sourceFile.replace(/[^A-Za-z0-9]/g, "-")}`

  async function remove() {
    setDeleting(true)
    setDeleteError(null)
    try {
      await api.deleteStatement(s.sourceFile)
      onDeleted()
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e))
      setDeleting(false)
    }
  }

  return (
    <>
      <TableRow className={cn(open && "bg-[var(--color-plane)]")}>
        <TableCell className="pr-0">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            aria-controls={panelId}
            aria-label={`${open ? "Hide" : "Show"} detail for ${s.sourceFile}`}
            className="grid size-6 place-items-center rounded text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]"
          >
            {open ? (
              <ChevronDown className="size-4" aria-hidden />
            ) : (
              <ChevronRight className="size-4" aria-hidden />
            )}
          </button>
        </TableCell>

        <TableCell className="whitespace-nowrap">{periodLabel(s)}</TableCell>

        <TableCell className="max-w-[18rem] truncate text-[var(--color-ink-secondary)]">
          {s.hasFile ? (
            <a
              href={api.originalUrl(s.sourceFile)}
              target="_blank"
              rel="noreferrer"
              title={`Open ${s.sourceFile}`}
              className="underline decoration-[var(--color-hairline)] underline-offset-2 hover:text-[var(--color-ink)] hover:decoration-current"
            >
              {s.sourceFile}
            </a>
          ) : (
            <span title={s.sourceFile}>{s.sourceFile}</span>
          )}
        </TableCell>

        <TableCell className="tnum text-right">{s.transactionCount}</TableCell>

        <TableCell className="tnum text-right">{money(s.chargesExtracted, true)}</TableCell>

        <TableCell>
          <StatusCell statement={s} />
        </TableCell>

        <TableCell className="pl-0 whitespace-nowrap">
          {s.hasFile && (
            <Button
              variant="ghost"
              size="icon"
              className="size-7 text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]"
              asChild
            >
              <a
                href={api.originalUrl(s.sourceFile)}
                target="_blank"
                rel="noreferrer"
                aria-label={`View ${s.sourceFile}`}
                title="View the statement"
              >
                <FileText />
              </a>
            </Button>
          )}
          <Button
            data-write
            variant="ghost"
            size="icon"
            className="size-7 text-[var(--color-ink-muted)] hover:text-[var(--color-critical)]"
            aria-label={`Remove ${s.sourceFile}`}
            onClick={onAskDelete}
          >
            <Trash2 />
          </Button>
        </TableCell>
      </TableRow>

      {open && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={7} className="bg-[var(--color-plane)] p-0">
            <div id={panelId} className="space-y-4 px-4 py-4 sm:px-5">
              <Detail statement={s} />

              {confirming ? (
                <div className="rounded-md border border-[var(--color-critical)]/40 bg-[var(--color-critical)]/5 px-3 py-3">
                  <p className="text-xs leading-relaxed">
                    Remove this statement? Its {s.transactionCount} transactions are
                    deleted from the store and the PDF is removed from the statement
                    folder. Every total in the app is recomputed without them.
                  </p>
                  {deleteError && (
                    <p className="mt-2 font-mono text-xs text-[var(--color-critical)]">
                      {deleteError}
                    </p>
                  )}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => void remove()}
                      disabled={deleting}
                    >
                      {deleting && <Spinner />}
                      {deleting ? "Removing" : "Remove statement"}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={onCancelDelete}
                      disabled={deleting}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {s.hasFile && (
                    <Button variant="outline" size="sm" asChild>
                      <a href={api.originalUrl(s.sourceFile)} target="_blank" rel="noreferrer">
                        <FileText />
                        View statement
                      </a>
                    </Button>
                  )}
                  <Button
                    data-write
                    variant="ghost"
                    size="sm"
                    className="text-[var(--color-critical)]"
                    onClick={onAskDelete}
                  >
                    <Trash2 />
                    Remove statement
                  </Button>
                </div>
              )}
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

function StatusCell({ statement: s }: { statement: Statement }) {
  if (s.accountType === "investment") {
    return (
      <Badge variant="neutral" title={s.reconcileNote}>
        Value {money(s.newBalance)}
      </Badge>
    )
  }
  if (s.status === "ok") {
    return (
      <Badge variant="good">
        <CircleCheck aria-hidden />
        Reconciles
      </Badge>
    )
  }

  if (s.status === "mismatch") {
    return (
      <div className="space-y-1">
        <Badge variant="critical">
          <CircleAlert aria-hidden />
          Off by {money(Math.abs(s.reconcileDiff ?? 0), true)}
        </Badge>
        {s.reconcileNote && (
          <p className="max-w-[22rem] text-xs leading-relaxed text-[var(--color-ink-secondary)]">
            {s.reconcileNote}
          </p>
        )}
      </div>
    )
  }

  return (
    <Hint label="This statement prints no summary total, so the extraction had nothing to be checked against. Not a failure — an unverified figure.">
      <span>
        <Badge variant="warning">
          <CircleHelp aria-hidden />
          No printed total
        </Badge>
      </span>
    </Hint>
  )
}

/* ------------------------------------------------------------------ detail */

function Detail({ statement: s }: { statement: Statement }) {
  const printed = s.purchases
  const extracted = s.chargesExtracted
  const creditsPrinted = s.payments === null ? null : Math.abs(s.payments)

  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-3 lg:grid-cols-4">
        <Field label="Statement period" value={s.period || "—"} />
        <Field
          label="Previous balance"
          value={money(s.previousBalance, true)}
          numeric
        />
        <Field label="New balance" value={money(s.newBalance, true)} numeric />
        <Field label="Credit limit" value={money(s.creditLimit, true)} numeric />
        <Field
          label="Purchase APR"
          value={s.purchaseApr === null ? "—" : `${s.purchaseApr.toFixed(2)}%`}
          numeric
        />
        <Field label="Interest charged" value={money(s.interestCharged, true)} numeric />
        <Field label="Fees charged" value={money(s.feesCharged, true)} numeric />
        {s.paymentDue && <Field label="Payment due" value={dateLabel(s.paymentDue)} />}
        <Field label="Transactions read" value={String(s.transactionCount)} numeric />
      </dl>

      <div className="rounded-md border border-[var(--color-hairline)] bg-[var(--color-surface)] p-3">
        <p className="text-xs font-medium">Extracted against printed</p>
        <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-4">
          <Field label="Charges extracted" value={money(extracted, true)} numeric />
          <Field label="Purchases printed" value={money(printed, true)} numeric />
          <Field label="Credits extracted" value={money(s.creditsExtracted, true)} numeric />
          <Field label="Payments printed" value={money(creditsPrinted, true)} numeric />
        </dl>
        <p className="mt-2.5 text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          {s.status === "ok" &&
            "The charges read off this statement add up to the total it prints, to the cent."}
          {s.status === "mismatch" &&
            `The two disagree by ${money(Math.abs(s.reconcileDiff ?? 0), true)}. Until that is explained, anything computed from this file is unverified.`}
          {s.status === "unchecked" &&
            "This statement prints no total for its own charges, so the extraction could not be checked. The figures may well be right; nothing here proves it."}
        </p>
        {s.reconcileNote && (
          <p className="mt-1.5 text-xs leading-relaxed text-[var(--color-ink-muted)]">
            {s.reconcileNote}
          </p>
        )}
      </div>

      <p className="font-mono text-xs break-all text-[var(--color-ink-muted)]">
        {s.sourceFile}
      </p>
    </div>
  )
}

function Field({
  label,
  value,
  numeric = false,
}: {
  label: string
  value: string
  numeric?: boolean
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[var(--color-ink-muted)]">{label}</dt>
      <dd className={cn("mt-0.5 break-words", numeric && "tnum")}>{value}</dd>
    </div>
  )
}
