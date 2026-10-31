import {
  CircleAlert,
  CircleCheck,
  CircleHelp,
  FileText,
  Loader2,
  UploadCloud,
  X,
} from "lucide-react"
import * as React from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/misc"
import { api } from "@/lib/api"
import { dateLabel, money } from "@/lib/format"
import type { Coverage, UploadResult } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * Drop statement PDFs in; get back what the parser actually made of them.
 *
 * The API parses on upload, so the response already carries the reconciliation
 * verdict. That makes "uploaded" the wrong thing to report: what the user needs
 * to know is whether the extracted charges agree with the total the statement
 * prints. A file that lands but does not reconcile is called out, never folded
 * into a success count.
 *
 * Files go up one at a time rather than in a single request, because parsing is
 * the slow part and a per-file verdict arriving as soon as it is known beats one
 * bar that sits at 100% while the server works through a batch.
 */

type Phase = "waiting" | "sending" | "parsing"

interface QueueItem {
  name: string
  pct: number
  phase: Phase
}

export function UploadDropzone({
  onUploaded,
  variant = "bar",
  className,
}: {
  /** Called after each file that made it into the store. */
  onUploaded: () => void
  /** `full` takes over the page when there is nothing in the store yet. */
  variant?: "bar" | "full"
  className?: string
}) {
  const inputRef = React.useRef<HTMLInputElement>(null)
  const dragDepth = React.useRef(0)
  const running = React.useRef(false)

  const [dragging, setDragging] = React.useState(false)
  const [queue, setQueue] = React.useState<QueueItem[]>([])
  const [results, setResults] = React.useState<UploadResult[]>([])
  const [coverage, setCoverage] = React.useState<Coverage | null>(null)

  const busy = queue.length > 0

  const start = React.useCallback(
    async (picked: File[]) => {
      if (running.current || picked.length === 0) return

      // The API refuses anything that is not a PDF anyway; saying so before the
      // round trip gives the same answer without the wait.
      const pdfs = picked.filter((f) => /\.pdf$/i.test(f.name))
      const notPdfs = picked.filter((f) => !/\.pdf$/i.test(f.name))

      running.current = true
      setCoverage(null)
      setResults(
        notPdfs.map((f) => ({
          ok: false,
          file: f.name,
          error: "Only PDF statements can be uploaded.",
        })),
      )
      setQueue(pdfs.map((f) => ({ name: f.name, pct: 0, phase: "waiting" as Phase })))

      for (let i = 0; i < pdfs.length; i++) {
        const file = pdfs[i]
        setQueue((q) =>
          q.map((it, j) => (j === i ? { ...it, phase: "sending", pct: 0 } : it)),
        )
        try {
          const res = await api.upload([file], (pct) =>
            setQueue((q) =>
              q.map((it, j) =>
                j === i
                  ? { ...it, pct, phase: pct >= 100 ? "parsing" : "sending" }
                  : it,
              ),
            ),
          )
          // The bytes are up; the server is now running the parser.
          setQueue((q) =>
            q.map((it, j) => (j === i ? { ...it, pct: 100, phase: "parsing" } : it)),
          )
          setResults((r) => [...r, ...res.results])
          const last = res.results[res.results.length - 1]
          if (last?.coverage) setCoverage(last.coverage)
          if (res.results.some((r) => r.ok)) onUploaded()
        } catch (e) {
          setResults((r) => [
            ...r,
            {
              ok: false,
              file: file.name,
              error: e instanceof Error ? e.message : String(e),
            },
          ])
        }
      }

      setQueue([])
      running.current = false
    },
    [onUploaded],
  )

  function onDrop(e: React.DragEvent) {
    e.preventDefault()
    dragDepth.current = 0
    setDragging(false)
    void start(Array.from(e.dataTransfer.files))
  }

  const zone = (
    <div
      onDragEnter={(e) => {
        e.preventDefault()
        dragDepth.current += 1
        setDragging(true)
      }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={(e) => {
        e.preventDefault()
        dragDepth.current -= 1
        if (dragDepth.current <= 0) setDragging(false)
      }}
      onDrop={onDrop}
      className={cn(
        "rounded-[var(--radius-card)] border border-dashed border-[var(--color-baseline)] transition-colors",
        dragging &&
          "border-solid border-[var(--color-series-1)] bg-[color-mix(in_oklab,var(--color-series-1)_8%,transparent)]",
      )}
    >
      <div
        className={cn(
          "flex flex-wrap items-center gap-x-4 gap-y-3",
          variant === "full"
            ? "flex-col justify-center px-6 py-14 text-center"
            : "px-4 py-3.5 sm:px-5",
        )}
      >
        <UploadCloud
          className={cn(
            "shrink-0 text-[var(--color-ink-muted)]",
            variant === "full" ? "size-8" : "size-5",
            dragging && "text-[var(--color-series-1)]",
          )}
          strokeWidth={1.5}
          aria-hidden
        />

        <div className={cn("min-w-0", variant === "full" ? "space-y-1" : "flex-1")}>
          <p className="text-sm font-medium">
            {dragging ? "Drop to add these statements" : "Add statement PDFs"}
          </p>
          <p
            className={cn(
              "text-xs leading-relaxed text-[var(--color-ink-secondary)]",
              variant === "full" && "mx-auto max-w-md",
            )}
          >
            {variant === "full" ? (
              <>
                Drop the statement PDFs you downloaded from your bank or card issuer —
                or pick them from disk. Each one is parsed on arrival and its
                transactions are checked against the totals the statement prints, so
                you can see straight away whether the extraction can be trusted.
                Nothing is sent anywhere; the parsing happens on this machine.
              </>
            ) : (
              <>
                Drop them here, or choose files. Each is parsed on arrival and checked
                against the totals printed on the statement.
              </>
            )}
          </p>
        </div>

        <Button
          variant={variant === "full" ? "default" : "outline"}
          size={variant === "full" ? "default" : "sm"}
          onClick={() => inputRef.current?.click()}
          disabled={busy}
        >
          {busy ? <Loader2 className="animate-spin" aria-hidden /> : null}
          {busy ? "Working" : "Choose files"}
        </Button>

        <input
          ref={inputRef}
          type="file"
          accept=".pdf,application/pdf"
          multiple
          className="sr-only"
          aria-label="Statement PDFs to upload"
          onChange={(e) => {
            void start(Array.from(e.target.files ?? []))
            e.target.value = ""
          }}
        />
      </div>

      {queue.length > 0 && (
        <ul className="space-y-2 border-t border-[var(--color-hairline)] px-4 py-3 sm:px-5">
          {queue.map((it) => (
            <li key={it.name} className="space-y-1.5">
              <div className="flex items-baseline justify-between gap-3 text-xs">
                <span className="min-w-0 flex-1 truncate">{it.name}</span>
                <span className="tnum shrink-0 text-[var(--color-ink-muted)]">
                  {it.phase === "waiting"
                    ? "queued"
                    : it.phase === "sending"
                      ? `${it.pct}%`
                      : "parsing"}
                </span>
              </div>
              <Progress
                value={it.phase === "waiting" ? 0 : it.pct}
                aria-label={`Upload progress for ${it.name}`}
                indicatorClassName={
                  it.phase === "parsing"
                    ? "animate-pulse bg-[var(--color-series-1)]"
                    : undefined
                }
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )

  return (
    <section aria-label="Add statements" className={cn("space-y-3", className)}>
      {zone}

      {results.length > 0 && (
        <div className="rounded-[var(--radius-card)] border border-[var(--color-hairline)] bg-[var(--color-surface)]">
          <div className="flex items-center justify-between gap-3 border-b border-[var(--color-hairline)] px-4 py-2.5">
            <h3 className="text-xs font-medium">
              {results.length} file{results.length === 1 ? "" : "s"} processed
            </h3>
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="Dismiss upload results"
              onClick={() => {
                setResults([])
                setCoverage(null)
              }}
            >
              <X />
            </Button>
          </div>

          <ul className="divide-y divide-[var(--color-hairline)]" aria-live="polite">
            {results.map((r, i) => (
              <li key={`${r.file}-${i}`} className="px-4 py-3">
                <ResultRow result={r} />
              </li>
            ))}
          </ul>

          {coverage && (
            <p className="border-t border-[var(--color-hairline)] px-4 py-2.5 text-xs text-[var(--color-ink-secondary)]">
              The store now holds {coverage.statements} statements;{" "}
              {coverage.reconciled} reconcile against their own printed totals
              {coverage.mismatched > 0 && `, ${coverage.mismatched} do not`}
              {coverage.unchecked > 0 &&
                `, ${coverage.unchecked} printed no total to check`}
              .
            </p>
          )}
        </div>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------ result */

function ResultRow({ result }: { result: UploadResult }) {
  const s = result.statement

  if (!result.ok) {
    return (
      <div className="space-y-1.5">
        <div className="flex items-start gap-2">
          <CircleAlert
            className="mt-0.5 size-4 shrink-0 text-[var(--color-critical)]"
            aria-hidden
          />
          <div className="min-w-0 space-y-0.5">
            <p className="text-sm font-medium break-words">{result.file}</p>
            <p className="text-xs text-[var(--color-ink-secondary)]">
              {result.error ?? "Could not parse this statement."} Nothing from this file
              was added to the store.
            </p>
          </div>
        </div>
        {result.log && <LogDetails log={result.log} />}
      </div>
    )
  }

  if (!s) {
    return (
      <div className="flex items-start gap-2">
        <CircleHelp
          className="mt-0.5 size-4 shrink-0 text-[var(--color-warning)]"
          aria-hidden
        />
        <div className="min-w-0 space-y-0.5">
          <p className="text-sm font-medium break-words">{result.file}</p>
          <p className="text-xs text-[var(--color-ink-secondary)]">
            The parser ran without error, but no statement record came back — so there
            is nothing to reconcile against. Reload to see what landed.
          </p>
        </div>
      </div>
    )
  }

  const n = result.transactionCount ?? s.transactionCount
  const period =
    s.periodStart && s.periodEnd
      ? `${dateLabel(s.periodStart)} – ${dateLabel(s.periodEnd)}`
      : s.period || "period not printed"

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <FileText className="size-4 shrink-0 text-[var(--color-ink-muted)]" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{result.file}</span>
        <StatusBadge statement={s} />
      </div>

      <p className="text-xs leading-relaxed text-[var(--color-ink-secondary)]">
        {s.issuer} ····{s.accountLast4} · {period} · {n} transaction
        {n === 1 ? "" : "s"}.{" "}
        {s.status === "ok" ? (
          <>
            Charges extracted come to {money(s.chargesExtracted, true)}, exactly the
            total this statement prints.
          </>
        ) : s.status === "mismatch" ? (
          <span className="text-[var(--color-critical)]">
            The charges extracted ({money(s.chargesExtracted, true)}) do not match the
            total printed on the statement — a gap of{" "}
            {money(Math.abs(s.reconcileDiff ?? 0), true)}. Treat figures from this file
            as unverified until it is resolved.
          </span>
        ) : (
          <>
            This statement prints no summary total, so there was nothing to check the
            extraction against. It is not wrong; it is unverified.
          </>
        )}
      </p>

      {s.reconcileNote && (
        <p className="text-xs text-[var(--color-ink-muted)]">{s.reconcileNote}</p>
      )}

      {result.replaced && (
        <p className="text-xs text-[var(--color-ink-muted)]">
          A file of this name was already stored. It was replaced, and its transactions
          were re-read from the new copy.
        </p>
      )}

      {result.log && <LogDetails log={result.log} />}
    </div>
  )
}

function StatusBadge({ statement }: { statement: NonNullable<UploadResult["statement"]> }) {
  if (statement.status === "ok") {
    return (
      <Badge variant="good">
        <CircleCheck aria-hidden />
        Reconciles
      </Badge>
    )
  }
  if (statement.status === "mismatch") {
    return (
      <Badge variant="critical">
        <CircleAlert aria-hidden />
        Off by {money(Math.abs(statement.reconcileDiff ?? 0), true)}
      </Badge>
    )
  }
  return (
    <Badge variant="warning">
      <CircleHelp aria-hidden />
      No printed total
    </Badge>
  )
}

function LogDetails({ log }: { log: string }) {
  return (
    <details>
      <summary className="cursor-pointer text-xs text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]">
        Parser output
      </summary>
      <pre className="scroll-x mt-1.5 max-h-56 overflow-y-auto rounded-md bg-[var(--color-plane)] px-3 py-2 font-mono text-[11px] leading-relaxed whitespace-pre">
        {log.trim()}
      </pre>
    </details>
  )
}
