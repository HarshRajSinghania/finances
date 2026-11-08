import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  Info,
  Inbox,
  ListFilter,
  Search,
  X,
} from "lucide-react"
import type * as React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardFooter } from "@/components/ui/card"
import { Hint, Input } from "@/components/ui/misc"
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
import { useApi, useDebounced } from "@/hooks/useApi"
import { api } from "@/lib/api"
import { dateLabel, fileLabel, money } from "@/lib/format"
import type { Transaction, TransactionQuery } from "@/lib/types"
import { cn } from "@/lib/utils"

/*
  The transaction browser.

  Every filter, the sort and the page live in the query string, so a view is a
  link: `?q=uber&spend=1&sort=amount` reloads into exactly the same 412 rows.
  All of the work happens in api.py — 1234 rows is small enough to fetch whole
  and large enough that doing so on every keystroke would be the wrong habit to
  build, and the server already sorts, filters, counts and totals.
*/

type SortKey = "date" | "amount" | "merchant" | "category"

const SORT_KEYS: SortKey[] = ["date", "amount", "merchant", "category"]
const PAGE_SIZES = [50, 100, 250]
const DEFAULT_LIMIT = 100

/** New sort column, new sensible direction: newest and largest first, names A–Z. */
const DEFAULT_ORDER: Record<SortKey, "asc" | "desc"> = {
  date: "desc",
  amount: "desc",
  merchant: "asc",
  category: "asc",
}

const SPEND_ONLY_EXPLANATION =
  "Excludes card payments, transfers, refunds and income — rows that move money " +
  "without spending it. They are real transactions, but counting them makes the " +
  "total meaningless, so this changes the figure a lot."

export function Transactions() {
  const [params, setParams] = useSearchParams()

  /* ------------------------------------------------------- URL is the state */

  const search = params.get("q") ?? ""
  const category = params.get("category") ?? ""
  const sourceFile = params.get("source") ?? ""
  const directionParam = params.get("direction")
  const direction = directionParam === "in" || directionParam === "out" ? directionParam : ""
  const start = params.get("start") ?? ""
  const end = params.get("end") ?? ""
  const spendOnly = params.get("spend") === "1"

  const sortParam = params.get("sort") as SortKey | null
  const sort: SortKey = sortParam && SORT_KEYS.includes(sortParam) ? sortParam : "date"
  const order: "asc" | "desc" = params.get("order") === "asc" ? "asc" : "desc"

  const limitParam = Number(params.get("limit"))
  const limit = PAGE_SIZES.includes(limitParam) ? limitParam : DEFAULT_LIMIT
  const offsetParam = Number(params.get("offset"))
  const offset = Number.isFinite(offsetParam) && offsetParam > 0 ? Math.floor(offsetParam) : 0

  /**
   * Any filter change drops you back to the first page. Otherwise you keep
   * page 7 of a result that is now two pages long and see nothing at all.
   */
  const update = useCallback(
    (patch: Record<string, string | null>, replace = false) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          for (const [k, v] of Object.entries(patch)) {
            if (v === null || v === "") next.delete(k)
            else next.set(k, v)
          }
          if (!("offset" in patch)) next.delete("offset")
          return next
        },
        { replace },
      )
    },
    [setParams],
  )

  /* ------------------------------------------------------------ search box */

  // The input is local so it stays responsive; only the settled value reaches
  // the URL and the API. `replace` keeps typing out of the history stack.
  const [searchInput, setSearchInput] = useState(search)
  const debouncedSearch = useDebounced(searchInput, 250)

  const clearAll = useCallback(() => {
    setSearchInput("")
    setParams(new URLSearchParams(), { replace: false })
  }, [setParams])

  useEffect(() => {
    // Only push a value that has finished settling. Without this first guard,
    // clearing the filters loses the race: `searchInput` empties immediately
    // while `debouncedSearch` still holds the old term for another 250ms, and
    // that stale value lands afterwards and puts the search straight back.
    if (debouncedSearch !== searchInput) return
    if (debouncedSearch === search) return
    update({ q: debouncedSearch || null }, true)
  }, [debouncedSearch, searchInput, search, update])

  /* ---------------------------------------------------------------- fetch */

  const query = useMemo<TransactionQuery>(
    () => ({
      search: search || undefined,
      category: category || undefined,
      sourceFile: sourceFile || undefined,
      direction: direction || undefined,
      start: start || undefined,
      end: end || undefined,
      spendOnly: spendOnly || undefined,
      sort,
      order,
      limit,
      offset,
    }),
    [search, category, sourceFile, direction, start, end, spendOnly, sort, order, limit, offset],
  )

  const { data, error, loading, refetch } = useApi(() => api.transactions(query), [query])

  const categoryOptions = useCategoryOptions()

  // Rows ticked for a bulk change, kept across pages and filters until applied.
  const [selected, setSelected] = useState<Map<string, Transaction>>(new Map())
  const toggle = (t: Transaction) =>
    setSelected((prev) => {
      const next = new Map(prev)
      if (next.has(rowKey(t))) next.delete(rowKey(t))
      else next.set(rowKey(t), t)
      return next
    })

  // A linked or reloaded URL can point past the end of a shorter result.
  useEffect(() => {
    if (!data || offset === 0) return
    if (offset < data.total) return
    const last = Math.max(0, Math.floor(Math.max(0, data.total - 1) / limit) * limit)
    update({ offset: last ? String(last) : null }, true)
  }, [data, offset, limit, update])

  /* --------------------------------------------------------------- derived */

  const activeFilters = buildChips({
    search,
    category,
    sourceFile,
    direction,
    start,
    end,
    spendOnly,
  })

  const hasFilters = activeFilters.length > 0

  const onSort = (col: SortKey) => {
    if (col === sort) update({ order: order === "asc" ? "desc" : "asc" })
    else update({ sort: col, order: DEFAULT_ORDER[col] })
  }

  const rows = data?.transactions ?? []
  const total = data?.total ?? 0
  const showingFrom = total === 0 ? 0 : offset + 1
  const showingTo = Math.min(offset + limit, total)
  const page = Math.floor(offset / limit) + 1
  const pages = Math.max(1, Math.ceil(total / limit))

  return (
    <div className="space-y-4">
      {/* ------------------------------------------------------- filter bar */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1 basis-56">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-[var(--color-ink-muted)]"
              strokeWidth={1.75}
            />
            <Input
              type="search"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search merchant or statement text"
              aria-label="Search transactions"
              className="pl-8"
            />
          </div>

          <Select
            value={category}
            onChange={(v) => update({ category: v || null })}
            aria-label="Category"
            className="basis-44"
          >
            <option value="">All categories</option>
            <optgroup label="Spending">
              {categoryOptions.spend.map((c) => (
                <option key={c.category} value={c.category}>
                  {c.category} ({c.count})
                </option>
              ))}
            </optgroup>
            {categoryOptions.nonSpend.length > 0 && (
              <optgroup label="Not spending">
                {categoryOptions.nonSpend.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </optgroup>
            )}
            {category && !categoryOptions.known.has(category) && (
              <option value={category}>{category}</option>
            )}
          </Select>

          <Select
            value={direction}
            onChange={(v) => update({ direction: v || null })}
            aria-label="Direction"
            className="basis-36"
          >
            <option value="">In and out</option>
            <option value="out">Money out</option>
            <option value="in">Money in</option>
          </Select>

          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant={spendOnly ? "default" : "outline"}
              size="sm"
              aria-pressed={spendOnly}
              onClick={() => update({ spend: spendOnly ? null : "1" })}
              className="h-9"
            >
              Spending only
            </Button>
            <Hint label={SPEND_ONLY_EXPLANATION}>
              <button
                type="button"
                aria-label="What “spending only” excludes"
                className="grid size-7 place-items-center rounded-md text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]"
              >
                <Info className="size-4" strokeWidth={1.75} />
              </button>
            </Hint>
          </div>

          <div className="flex items-center gap-1.5">
            <Input
              type="date"
              value={start}
              max={end || undefined}
              onChange={(e) => update({ start: e.target.value || null })}
              aria-label="From date"
              className="w-[9.5rem]"
            />
            <span className="text-xs text-[var(--color-ink-muted)]">to</span>
            <Input
              type="date"
              value={end}
              min={start || undefined}
              onChange={(e) => update({ end: e.target.value || null })}
              aria-label="To date"
              className="w-[9.5rem]"
            />
          </div>
        </div>

        {hasFilters && (
          <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-[var(--color-hairline)] pt-3">
            <ListFilter
              className="size-3.5 text-[var(--color-ink-muted)]"
              strokeWidth={1.75}
              aria-hidden="true"
            />
            <span className="mr-1 text-xs text-[var(--color-ink-muted)]">
              {activeFilters.length} {activeFilters.length === 1 ? "filter" : "filters"}
            </span>
            {activeFilters.map((f) => (
              <button
                key={f.key}
                type="button"
                onClick={() => {
                  if (f.key === "q") setSearchInput("")
                  update({ [f.param]: null })
                }}
                aria-label={`Remove filter: ${f.label}`}
                className="inline-flex max-w-[18rem] items-center gap-1 rounded-full border border-[var(--color-hairline)] bg-[var(--color-plane)] py-0.5 pr-1.5 pl-2.5 text-xs text-[var(--color-ink-secondary)] transition-colors hover:text-[var(--color-ink)]"
              >
                <span className="truncate">{f.label}</span>
                <X className="size-3 shrink-0" strokeWidth={2} />
              </button>
            ))}
            <Button variant="ghost" size="sm" onClick={clearAll} className="ml-auto">
              Clear all
            </Button>
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------ table */}
      <Card>
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-5 py-3.5">
          <p className="flex items-center gap-2 text-sm">
            {data ? (
              <>
                <span className="tnum font-medium">{total.toLocaleString()}</span>
                <span className="text-[var(--color-ink-secondary)]">
                  {total === 1 ? "transaction" : "transactions"}
                  {hasFilters ? " match these filters" : ""}
                </span>
              </>
            ) : (
              <span className="text-[var(--color-ink-secondary)]">Counting…</span>
            )}
            {loading && data && <Spinner className="text-[var(--color-ink-muted)]" />}
          </p>

          {data && total > 0 && <SumFigure sum={data.sumAmount} spendOnly={spendOnly} direction={direction} />}
        </div>

        {error ? (
          <div className="border-t border-[var(--color-hairline)]">
            <ErrorState error={error} onRetry={refetch} />
          </div>
        ) : !data ? (
          <div className="border-t border-[var(--color-hairline)]">
            <LoadingRows rows={8} />
          </div>
        ) : total === 0 ? (
          <div className="border-t border-[var(--color-hairline)]">
            {hasFilters ? (
              <EmptyState
                icon={Search}
                title="No transactions match these filters"
                body="Every row was excluded by the filter bar above. Widen the date range or clear the filters to see the full store."
                action={
                  <Button variant="outline" size="sm" onClick={clearAll}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon={Inbox}
                title="No transactions in the store"
                body="Nothing has been parsed yet. Add statement PDFs and they are read into the store immediately."
                action={
                  <Button variant="outline" size="sm" asChild>
                    <Link to="/statements">Go to statements</Link>
                  </Button>
                }
              />
            )}
          </div>
        ) : (
          <>
            {selected.size > 0 && (
              <BulkCategory
                selected={[...selected.values()]}
                options={[...categoryOptions.known].sort()}
                onClear={() => setSelected(new Map())}
                onDone={() => {
                  setSelected(new Map())
                  refetch()
                }}
              />
            )}

            {/* Rows stay put and dim while a new page loads — a skeleton on
                every keystroke reads as a crash, not as progress. */}
            <div
              aria-busy={loading}
              className={cn(
                "border-t border-[var(--color-hairline)] transition-opacity duration-150",
                loading && "opacity-45",
              )}
            >
              <TableWrap>
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="w-8 pr-0" data-write>
                        <input
                          type="checkbox"
                          aria-label="Select every row on this page"
                          checked={rows.length > 0 && rows.every((t) => selected.has(rowKey(t)))}
                          onChange={(e) =>
                            setSelected((prev) => {
                              const next = new Map(prev)
                              for (const t of rows) {
                                if (e.target.checked) next.set(rowKey(t), t)
                                else next.delete(rowKey(t))
                              }
                              return next
                            })
                          }
                          className="size-3.5 accent-[var(--color-ink)]"
                        />
                      </TableHead>
                      <SortHeader label="Date" col="date" sort={sort} order={order} onSort={onSort} />
                      <SortHeader
                        label="Merchant"
                        col="merchant"
                        sort={sort}
                        order={order}
                        onSort={onSort}
                      />
                      <SortHeader
                        label="Category"
                        col="category"
                        sort={sort}
                        order={order}
                        onSort={onSort}
                      />
                      <SortHeader
                        label="Amount"
                        col="amount"
                        sort={sort}
                        order={order}
                        onSort={onSort}
                        align="right"
                      />
                      <TableHead>Account</TableHead>
                      <TableHead>Statement</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((t, i) => (
                      <Row
                        key={`${offset + i}|${t.sourceFile}|${t.date}|${t.description}|${t.amount}`}
                        t={t}
                        selected={selected.has(rowKey(t))}
                        onSelect={() => toggle(t)}
                        onCategory={(c) => update({ category: c })}
                        onSource={(s) => update({ source: s })}
                      />
                    ))}
                  </TableBody>
                </Table>
              </TableWrap>
            </div>

            <CardFooter className="flex-wrap justify-between gap-x-6 gap-y-3">
              <p className="tnum text-xs text-[var(--color-ink-secondary)]">
                Showing {showingFrom.toLocaleString()}–{showingTo.toLocaleString()} of{" "}
                {total.toLocaleString()}
              </p>

              <div className="flex flex-wrap items-center gap-4">
                <div
                  role="group"
                  aria-label="Rows per page"
                  className="flex items-center gap-1 text-xs"
                >
                  <span className="text-[var(--color-ink-muted)]">Rows</span>
                  <div className="flex rounded-md border border-[var(--color-hairline)] p-0.5">
                    {PAGE_SIZES.map((n) => (
                      <button
                        key={n}
                        type="button"
                        aria-pressed={n === limit}
                        onClick={() => update({ limit: String(n) })}
                        className={cn(
                          "tnum rounded-[5px] px-2 py-0.5 transition-colors",
                          n === limit
                            ? "bg-[var(--color-plane)] font-medium text-[var(--color-ink)]"
                            : "text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]",
                        )}
                      >
                        {n}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label="Previous page"
                    disabled={offset === 0}
                    onClick={() => {
                      const prev = Math.max(0, offset - limit)
                      update({ offset: prev ? String(prev) : null })
                    }}
                  >
                    <ChevronLeft />
                    Previous
                  </Button>
                  <span className="tnum text-xs whitespace-nowrap text-[var(--color-ink-secondary)]">
                    Page {page} of {pages}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label="Next page"
                    disabled={offset + limit >= total}
                    onClick={() => update({ offset: String(offset + limit) })}
                  >
                    Next
                    <ChevronRight />
                  </Button>
                </div>
              </div>
            </CardFooter>
          </>
        )}
      </Card>

      <p className="text-xs leading-relaxed text-[var(--color-ink-muted)]">
        Money out is positive; money in is marked <span className="text-[var(--color-ink-secondary)]">in</span>.
        Every row names the statement it was read from — expand a row to see the raw descriptor the
        statement actually printed.
      </p>
    </div>
  )
}

/* ------------------------------------------------------------------ a row */

function Row({
  t,
  selected,
  onSelect,
  onCategory,
  onSource,
}: {
  t: Transaction
  selected: boolean
  onSelect: () => void
  onCategory: (c: string) => void
  onSource: (s: string) => void
}) {
  const [open, setOpen] = useState(false)
  const isIn = t.amount < 0

  return (
    <>
      <TableRow className={cn((open || selected) && "bg-[var(--color-plane)]")}>
        <TableCell className="w-8 pr-0" data-write>
          <input
            type="checkbox"
            checked={selected}
            onChange={onSelect}
            aria-label={`Select ${t.merchant} on ${dateLabel(t.date)}`}
            className="size-3.5 accent-[var(--color-ink)]"
          />
        </TableCell>
        <TableCell className="tnum whitespace-nowrap text-[var(--color-ink-secondary)]">
          {dateLabel(t.date)}
        </TableCell>

        <TableCell className="max-w-[18rem] min-w-[11rem]">
          {/* The merchant is normalised; the statement printed something else.
              Hover for a peek, expand for the whole provenance of the row. */}
          <Hint label={<span className="font-mono">{t.description}</span>}>
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              className="flex w-full items-center gap-1.5 rounded text-left"
            >
              <ChevronDown
                className={cn(
                  "size-3.5 shrink-0 text-[var(--color-ink-muted)] transition-transform",
                  !open && "-rotate-90",
                )}
                strokeWidth={2}
                aria-hidden="true"
              />
              <span className="truncate">{t.merchant}</span>
              {t.items && t.items.length > 0 && (
                <span className="shrink-0 rounded border border-[var(--color-hairline)] px-1 text-[10px] text-[var(--color-ink-muted)]">
                  {t.items.length} item{t.items.length === 1 ? "" : "s"}
                </span>
              )}
              {t.sourceFile.startsWith("simplefin#") && (
                <span
                  title="From the live bank feed: confirmed when the statement arrives"
                  className="shrink-0 rounded border border-dashed border-[var(--color-baseline)] px-1 text-[10px] text-[var(--color-ink-muted)]"
                >
                  live
                </span>
              )}
            </button>
          </Hint>
        </TableCell>

        <TableCell>
          <button
            type="button"
            onClick={() => onCategory(t.category)}
            aria-label={`Filter to category ${t.category}`}
            className="rounded-full"
          >
            <Badge variant="neutral">{t.category}</Badge>
          </button>
        </TableCell>

        {/*
          Money out is positive and money in negative in the store. A bare minus
          in a column of charges is read as a discount, so money in is printed
          as its own magnitude and labelled. Not a status colour: it is a
          direction, not a problem.
        */}
        <TableCell className="text-right whitespace-nowrap">
          <span
            className={cn(
              "tnum",
              isIn ? "text-[var(--color-ink-secondary)]" : "font-medium",
            )}
          >
            {money(Math.abs(t.amount), true)}
          </span>
          {/* Reserves its width on every row so the figures still line up. */}
          <span
            aria-hidden={isIn ? undefined : true}
            className={cn(
              "ml-1.5 inline-block w-4 text-left text-[10px] font-medium tracking-wide text-[var(--color-ink-muted)] uppercase",
              !isIn && "invisible",
            )}
          >
            in
          </span>
        </TableCell>

        <TableCell className="whitespace-nowrap text-[var(--color-ink-secondary)]">
          {t.issuer} <span className="tnum text-[var(--color-ink-muted)]">····{t.accountLast4}</span>
        </TableCell>

        <TableCell className="max-w-[14rem]">
          <button
            type="button"
            onClick={() => onSource(t.sourceFile)}
            title={t.sourceFile}
            aria-label={`Filter to statement ${fileLabel(t.sourceFile)}`}
            className="block w-full truncate rounded text-left text-xs text-[var(--color-ink-secondary)] hover:text-[var(--color-ink)] hover:underline"
          >
            {fileLabel(t.sourceFile)}
          </button>
        </TableCell>
      </TableRow>

      {open && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={7} className="bg-[var(--color-plane)] px-3 py-3">
            <dl className="grid gap-x-8 gap-y-2 text-xs sm:grid-cols-[auto_1fr]">
              <dt className="text-[var(--color-ink-muted)]">Statement descriptor</dt>
              <dd className="font-mono break-words text-[var(--color-ink)]">{t.description}</dd>

              <dt className="text-[var(--color-ink-muted)]">Posted</dt>
              <dd className="tnum">
                {t.postDate ? dateLabel(t.postDate) : "not printed on the statement"}
              </dd>

              <dt className="text-[var(--color-ink-muted)]">Read from</dt>
              <dd className="break-words">
                {t.sourceFile.startsWith("simplefin#") ? (
                  "the live bank feed; replaced by the statement when it arrives"
                ) : (
                  <a
                    href={api.originalUrl(t.sourceFile) + (t.page ? `#page=${t.page}` : "")}
                    target="_blank"
                    rel="noreferrer"
                    className="underline decoration-[var(--color-hairline)] underline-offset-2 hover:decoration-current"
                  >
                    {t.sourceFile}
                    {t.page ? `, page ${t.page}` : ""}
                  </a>
                )}
              </dd>

              {t.items && t.items.length > 0 && (
                <>
                  <dt className="text-[var(--color-ink-muted)]">What was in it</dt>
                  <dd>
                    <ul className="space-y-0.5">
                      {t.items.map((it, i) => (
                        <li key={i} className="flex gap-3">
                          <span className="min-w-0 flex-1">
                            {it.quantity && it.quantity > 1 ? `${it.quantity} × ` : ""}
                            {it.name}
                          </span>
                          <span className="tnum">{money(it.amount, true)}</span>
                        </li>
                      ))}
                    </ul>
                  </dd>
                </>
              )}
            </dl>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

/* -------------------------------------------------------------- sub-parts */

function SortHeader({
  label,
  col,
  sort,
  order,
  onSort,
  align,
}: {
  label: string
  col: SortKey
  sort: SortKey
  order: "asc" | "desc"
  onSort: (c: SortKey) => void
  align?: "right"
}) {
  const active = sort === col
  const Icon = !active ? ChevronsUpDown : order === "asc" ? ArrowUp : ArrowDown

  return (
    <TableHead
      aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"}
      className={align === "right" ? "text-right" : undefined}
    >
      <button
        type="button"
        onClick={() => onSort(col)}
        className={cn(
          "inline-flex items-center gap-1 rounded transition-colors hover:text-[var(--color-ink)]",
          active && "font-semibold text-[var(--color-ink)]",
        )}
      >
        {label}
        <Icon
          className={cn("size-3", !active && "text-[var(--color-baseline)]")}
          strokeWidth={2}
          aria-hidden="true"
        />
      </button>
    </TableHead>
  )
}

/**
 * `sumAmount` is the signed sum of the filtered rows, so what it means depends
 * on what is in the set. Naming it wrong is worse than not showing it.
 */
function SumFigure({
  sum,
  spendOnly,
  direction,
}: {
  sum: number
  spendOnly: boolean
  direction: "" | "in" | "out"
}) {
  let value = money(sum)
  let label: string
  let explain: string

  if (spendOnly || direction === "out") {
    label = "spending"
    explain = "The sum of every charge in the filtered set."
  } else if (direction === "in") {
    value = money(Math.abs(sum))
    label = "money in"
    explain = "The sum of every payment, refund, transfer and deposit in the filtered set."
  } else {
    value = money(Math.abs(sum))
    label = sum >= 0 ? "net out" : "net in"
    explain =
      "The filtered set holds money going both ways, so this is the net: charges " +
      "minus payments, refunds and transfers."
  }

  return (
    <Hint label={explain}>
      <button type="button" className="flex items-baseline gap-1.5 rounded text-left">
        <span className="tnum text-sm font-semibold">{value}</span>
        <span className="text-xs text-[var(--color-ink-muted)]">{label}</span>
        <Info className="size-3 text-[var(--color-ink-muted)]" strokeWidth={2} aria-hidden="true" />
      </button>
    </Hint>
  )
}

function Select({
  value,
  onChange,
  children,
  className,
  ...rest
}: {
  value: string
  onChange: (v: string) => void
  children: React.ReactNode
  className?: string
} & Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "value" | "onChange" | "className">) {
  return (
    <div className={cn("relative min-w-0 flex-1", className)}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 w-full appearance-none rounded-md border border-[var(--color-hairline)] bg-[var(--color-surface)] pr-8 pl-3 text-sm text-[var(--color-ink)]"
        {...rest}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-[var(--color-ink-muted)]"
        strokeWidth={1.75}
        aria-hidden="true"
      />
    </div>
  )
}

/* ------------------------------------------------------------------ hooks */

/**
 * `api.categories()` is derived from spending, so it cannot name the categories
 * that only ever appear as money in — Payment, Income, Transfers/Cash,
 * Credits/Refunds and, in this store, Utilities. Filtering by them would be
 * impossible from a spend-only list, so the money-in rows are read once to
 * complete the vocabulary rather than hard-coding the server's constant here.
 */
function rowKey(t: Transaction): string {
  return `${t.date}|${t.description}|${t.amount}|${t.accountLast4}|${t.seq}`
}

/**
 * Move the ticked rows to one category. "Remember" teaches the categorizer the
 * merchants, so their other rows move too and later imports land there.
 */
function BulkCategory({
  selected,
  options,
  onClear,
  onDone,
}: {
  selected: Transaction[]
  options: string[]
  onClear: () => void
  onDone: () => void
}) {
  const [category, setCategory] = useState("")
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ text: string; bad?: boolean } | null>(null)
  const merchants = new Set(selected.map((t) => t.merchant)).size

  async function apply() {
    setBusy(true)
    setNote(null)
    try {
      const r = await api.setCategory(selected, category.trim(), remember)
      setNote({
        text:
          `Moved ${r.updated} to ${category.trim()}` +
          (r.alsoMoved ? `, and ${r.alsoMoved} more from the same merchants` : "") +
          (r.remembered.length ? `. Remembered for ${r.remembered.length} merchant${r.remembered.length === 1 ? "" : "s"}.` : "."),
      })
      setCategory("")
      onDone()
    } catch (e) {
      setNote({ text: e instanceof Error ? e.message : String(e), bad: true })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-[var(--color-hairline)] bg-[var(--color-plane)] px-4 py-2.5 text-sm" data-write>
      <span className="font-medium">{selected.length} selected</span>
      <form
        className="flex flex-wrap items-center gap-x-3 gap-y-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (category.trim()) void apply()
        }}
      >
        <Input
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          list="bulk-categories"
          placeholder="Move to category…"
          aria-label="Category to move them to"
          className="h-8 w-48"
        />
        <datalist id="bulk-categories">
          {options.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
        <label className="flex items-center gap-1.5 text-xs text-[var(--color-ink-secondary)]">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            className="size-3.5 accent-[var(--color-ink)]"
          />
          Remember for {merchants === 1 ? "this merchant" : `these ${merchants} merchants`}
        </label>
        <Button type="submit" size="sm" disabled={busy || !category.trim()}>
          {busy && <Spinner />}
          Move
        </Button>
      </form>
      <Button variant="ghost" size="sm" onClick={onClear} disabled={busy}>
        Clear
      </Button>
      {note && (
        <span
          className={cn(
            "basis-full text-xs",
            note.bad ? "text-[var(--color-critical)]" : "text-[var(--color-ink-secondary)]",
          )}
        >
          {note.text}
        </span>
      )}
    </div>
  )
}

function useCategoryOptions() {
  const { data: cats } = useApi(() => api.categories(), [])
  const { data: inbound } = useApi(
    () => api.transactions({ direction: "in", limit: 1000 }),
    [],
  )

  return useMemo(() => {
    const spend = cats?.categories ?? []
    const spendNames = new Set(spend.map((c) => c.category))
    const nonSpend = [
      ...new Set(
        (inbound?.transactions ?? [])
          .map((t) => t.category)
          .filter((c) => !spendNames.has(c)),
      ),
    ].sort()
    return { spend, nonSpend, known: new Set([...spendNames, ...nonSpend]) }
  }, [cats, inbound])
}

/* ---------------------------------------------------------------- helpers */

interface Chip {
  key: string
  param: string
  label: string
}

function buildChips(f: {
  search: string
  category: string
  sourceFile: string
  direction: "" | "in" | "out"
  start: string
  end: string
  spendOnly: boolean
}): Chip[] {
  const chips: Chip[] = []
  if (f.search) chips.push({ key: "q", param: "q", label: `“${f.search}”` })
  if (f.category) chips.push({ key: "category", param: "category", label: f.category })
  if (f.sourceFile)
    chips.push({ key: "source", param: "source", label: fileLabel(f.sourceFile) })
  if (f.direction)
    chips.push({
      key: "direction",
      param: "direction",
      label: f.direction === "in" ? "Money in" : "Money out",
    })
  if (f.spendOnly) chips.push({ key: "spend", param: "spend", label: "Spending only" })
  if (f.start) chips.push({ key: "start", param: "start", label: `From ${dateLabel(f.start)}` })
  if (f.end) chips.push({ key: "end", param: "end", label: `To ${dateLabel(f.end)}` })
  return chips
}
