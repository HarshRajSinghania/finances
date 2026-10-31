/**
 * Insights — the things worth a second look.
 *
 * Every section here is a question rather than a verdict. A charge that repeats
 * within a day might be a double-bill or it might be two coffees; a charge far
 * above a merchant's usual might be a genuine large purchase. The data cannot
 * tell the difference, so the page shows the evidence and says what it does not
 * know, rather than asserting fraud and being wrong.
 *
 * An empty section is good news and says so — rendering nothing would read as
 * "not checked".
 */

import { Copy, Flame, ReceiptText, Store, TrendingUp } from "lucide-react"
import type * as React from "react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Hint } from "@/components/ui/misc"
import { EmptyState, ErrorState, LoadingRows } from "@/components/ui/state"
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
import { dateLabel, money, relativeDay } from "@/lib/format"
import type { Anomalies, Merchant } from "@/lib/types"

export function Insights() {
  const anomalies = useApi(() => api.anomalies(), [])
  const merchants = useApi(() => api.merchants(25), [])

  return (
    <div className="space-y-6">
      <p className="max-w-2xl text-sm leading-relaxed text-[var(--color-ink-secondary)]">
        Patterns a category total will not surface. Nothing here is an
        accusation — each section says what it found and what it cannot tell you
        on its own.
      </p>

      <Section
        icon={Copy}
        title="Possible double charges"
        description="The same merchant, the same amount, within a day of itself."
        note="Often legitimate — two coffees, two fares, a split bill. Worth checking against the statement before disputing anything."
        state={anomalies}
        emptyTitle="No repeated charges found"
        emptyBody="No merchant billed the same amount twice within a day."
        isEmpty={(d) => d.duplicates.length === 0}
      >
        {(d) => <DuplicatesTable rows={d.duplicates} />}
      </Section>

      <Section
        icon={Flame}
        title="Unusual charges"
        description="Far above what this merchant normally costs you."
        note="Measured against that merchant's own median rather than a fixed threshold, so a place you always spend a lot at will not appear here."
        state={anomalies}
        emptyTitle="Nothing out of the ordinary"
        emptyBody="No charge stood far enough above its merchant's usual amount to flag."
        isEmpty={(d) => d.outliers.length === 0}
      >
        {(d) => <OutliersTable rows={d.outliers} />}
      </Section>

      <Section
        icon={ReceiptText}
        title="Largest charges"
        description="The biggest single transactions in the period."
        state={anomalies}
        emptyTitle="No spending recorded"
        emptyBody="There are no spending transactions in this store yet."
        isEmpty={(d) => d.largest.length === 0}
      >
        {(d) => <LargestTable rows={d.largest} />}
      </Section>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Store className="size-4 text-[var(--color-ink-muted)]" strokeWidth={1.75} />
            <CardTitle>Where it goes most often</CardTitle>
          </div>
          <CardDescription>
            Ranked by total spent. Names are grouped, so store numbers and
            payment-processor prefixes fold into one merchant.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0 pb-1">
          {merchants.loading && !merchants.data ? (
            <LoadingRows rows={6} />
          ) : merchants.error ? (
            <ErrorState error={merchants.error} onRetry={merchants.refetch} />
          ) : !merchants.data?.merchants.length ? (
            <EmptyState
              title="No merchants yet"
              body="Parse some statements and they will appear here."
            />
          ) : (
            <MerchantsTable rows={merchants.data.merchants} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/* ------------------------------------------------------------------ shell */

function Section<T>({
  icon: Icon,
  title,
  description,
  note,
  state,
  isEmpty,
  emptyTitle,
  emptyBody,
  children,
}: {
  icon: React.ElementType
  title: string
  description: string
  note?: string
  state: {
    data: T | null
    error: Error | null
    loading: boolean
    refetch: () => void
  }
  isEmpty: (d: T) => boolean
  emptyTitle: string
  emptyBody: string
  children: (d: T) => React.ReactNode
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Icon className="size-4 text-[var(--color-ink-muted)]" strokeWidth={1.75} />
          <CardTitle>{title}</CardTitle>
        </div>
        <CardDescription>{description}</CardDescription>
        {note && (
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[var(--color-ink-muted)]">
            {note}
          </p>
        )}
      </CardHeader>
      <CardContent className="p-0 pb-1">
        {state.loading && !state.data ? (
          <LoadingRows rows={4} />
        ) : state.error ? (
          <ErrorState error={state.error} onRetry={state.refetch} />
        ) : !state.data || isEmpty(state.data) ? (
          <EmptyState title={emptyTitle} body={emptyBody} />
        ) : (
          children(state.data)
        )}
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------------ tables */

function DuplicatesTable({ rows }: { rows: Anomalies["duplicates"] }) {
  return (
    <TableWrap>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Merchant</TableHead>
            <TableHead>First seen</TableHead>
            <TableHead className="text-right">Times</TableHead>
            <TableHead className="text-right">Each</TableHead>
            <TableHead className="text-right">Together</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((d, i) => (
            <TableRow key={`${d.name}-${d.date}-${i}`}>
              <TableCell className="font-medium">{d.name}</TableCell>
              <TableCell className="whitespace-nowrap text-[var(--color-ink-secondary)]">
                {dateLabel(d.date)}
              </TableCell>
              <TableCell className="tnum text-right">{d.n}</TableCell>
              <TableCell className="tnum text-right">{money(d.amount, true)}</TableCell>
              <TableCell className="tnum text-right font-medium">
                {money(d.amount * d.n, true)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableWrap>
  )
}

function OutliersTable({ rows }: { rows: Anomalies["outliers"] }) {
  return (
    <TableWrap>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Merchant</TableHead>
            <TableHead>Date</TableHead>
            <TableHead>Category</TableHead>
            <TableHead className="text-right">Usually</TableHead>
            <TableHead className="text-right">This time</TableHead>
            <TableHead className="text-right">Multiple</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((o, i) => {
            const times = o.typical > 0 ? o.amount / o.typical : null
            return (
              <TableRow key={`${o.name}-${o.date}-${i}`}>
                <TableCell className="font-medium">{o.name}</TableCell>
                <TableCell className="whitespace-nowrap text-[var(--color-ink-secondary)]">
                  {dateLabel(o.date)}
                </TableCell>
                <TableCell className="text-[var(--color-ink-secondary)]">
                  {o.category}
                </TableCell>
                <TableCell className="tnum text-right text-[var(--color-ink-secondary)]">
                  {money(o.typical, true)}
                </TableCell>
                <TableCell className="tnum text-right font-medium">
                  {money(o.amount, true)}
                </TableCell>
                <TableCell className="text-right">
                  {times ? (
                    <Badge variant="outline" className="tnum">
                      <TrendingUp aria-hidden />
                      {times.toFixed(1)}×
                    </Badge>
                  ) : (
                    <span className="text-[var(--color-ink-muted)]">—</span>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </TableWrap>
  )
}

function LargestTable({ rows }: { rows: Anomalies["largest"] }) {
  return (
    <TableWrap>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Merchant</TableHead>
            <TableHead>Date</TableHead>
            <TableHead>Category</TableHead>
            <TableHead>From</TableHead>
            <TableHead className="text-right">Amount</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((t, i) => (
            <TableRow key={`${t.date}-${t.description}-${t.seq}-${i}`}>
              <TableCell className="max-w-[18rem]">
                <Hint label={t.description}>
                  <span className="block truncate font-medium">{t.merchant}</span>
                </Hint>
              </TableCell>
              <TableCell className="whitespace-nowrap text-[var(--color-ink-secondary)]">
                {dateLabel(t.date)}
              </TableCell>
              <TableCell className="text-[var(--color-ink-secondary)]">
                {t.category}
              </TableCell>
              {/* Provenance: which statement this line was read out of. */}
              <TableCell className="max-w-[12rem] text-xs text-[var(--color-ink-muted)]">
                <span className="block truncate" title={t.sourceFile}>
                  {t.sourceFile}
                </span>
              </TableCell>
              <TableCell className="tnum text-right font-medium">
                {money(t.amount, true)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableWrap>
  )
}

function MerchantsTable({ rows }: { rows: Merchant[] }) {
  const max = Math.max(...rows.map((m) => m.total), 1)
  return (
    <TableWrap>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Merchant</TableHead>
            <TableHead>Category</TableHead>
            <TableHead className="text-right">Visits</TableHead>
            <TableHead className="text-right">Average</TableHead>
            <TableHead className="text-right">Last</TableHead>
            <TableHead className="text-right">Total</TableHead>
            <TableHead className="w-24" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((m) => (
            <TableRow key={m.key}>
              <TableCell className="max-w-[16rem]">
                <span className="block truncate font-medium">{m.name}</span>
              </TableCell>
              <TableCell className="text-[var(--color-ink-secondary)]">
                {m.category}
              </TableCell>
              <TableCell className="tnum text-right">{m.n}</TableCell>
              <TableCell className="tnum text-right text-[var(--color-ink-secondary)]">
                {money(m.avg, true)}
              </TableCell>
              <TableCell className="text-right whitespace-nowrap text-[var(--color-ink-muted)]">
                {relativeDay(m.last)}
              </TableCell>
              <TableCell className="tnum text-right font-medium">
                {money(m.total)}
              </TableCell>
              <TableCell>
                {/* One measure, one hue — length carries the magnitude. */}
                <div
                  className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--color-plane)]"
                  role="img"
                  aria-label={`${m.name}: ${money(m.total)} total`}
                >
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max((m.total / max) * 100, 2)}%`,
                      background: "var(--color-series-1)",
                    }}
                  />
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableWrap>
  )
}
