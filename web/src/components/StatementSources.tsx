import { ArrowUpRight, CircleCheck, Download, FolderSearch, Loader2, Radio } from "lucide-react"
import * as React from "react"
import { Link } from "react-router-dom"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/misc"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import { dateLabel } from "@/lib/format"
import type { SourceAccount, Sources, WatchStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * Keeping the store current without a bank login.
 *
 * Two things, and nothing more: a switch that adds new statements as they land
 * in Downloads, and each account's next statement, with the bank's own page
 * one click away and the steps to get there for when its menus are a maze.
 */
export function StatementSources({ data, onChange }: { data: Sources; onChange: () => void }) {
  return (
    <Card className="divide-y divide-[var(--color-hairline)]">
      <div className="px-5 py-4">
        <h2 className="text-sm font-semibold tracking-tight">Keep it up to date</h2>
        <p className="mt-0.5 text-xs text-[var(--color-ink-secondary)]">
          Each bank posts a new statement about a month after the last. Download it
          from the bank's site and it is added here.
        </p>
      </div>
      <WatchRow watch={data.watch} onChange={onChange} />
      <FeedRow onChange={onChange} />
      <ul className="divide-y divide-[var(--color-hairline)]">
        {data.accounts.map((a) => (
          <AccountRow key={`${a.issuer}-${a.last4}`} a={a} />
        ))}
      </ul>
    </Card>
  )
}

function WatchRow({ watch, onChange }: { watch: WatchStatus; onChange: () => void }) {
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const last = watch.log.filter((e) => e.ok).at(-1)

  async function toggle() {
    setBusy(true)
    setError(null)
    try {
      await api.watch(watch.on ? null : watch.suggested)
      onChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!watch.on && !watch.suggested) return null
  const shown = (watch.folder || watch.suggested).replace(/^\/(Users|home)\/[^/]+/, "~")
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3" data-write>
      <FolderSearch className="size-4 shrink-0 text-[var(--color-ink-muted)]" aria-hidden />
      <div className="min-w-[14rem] flex-1 text-sm">
        <p>
          {watch.on ? (
            <>
              Adding new statements from <span className="font-mono text-xs">{shown}</span>{" "}
              as they arrive.
            </>
          ) : (
            <>
              Add new statements from <span className="font-mono text-xs">{shown}</span>{" "}
              automatically
            </>
          )}
        </p>
        <p className="text-xs text-[var(--color-ink-muted)]">
          {watch.on
            ? last
              ? `Last added: ${last.file}.`
              : "Only files that arrive from now on; anything that is not a statement is left alone."
            : "Only while this dashboard is running. Nothing else in the folder is touched."}
        </p>
        {(watch.error || error) && (
          <p className="text-xs text-[var(--color-critical)]">{watch.error || error}</p>
        )}
      </div>
      <Button size="sm" variant={watch.on ? "ghost" : "outline"} onClick={toggle} disabled={busy}>
        {busy && <Loader2 className="animate-spin" aria-hidden />}
        {watch.on ? "Stop" : "Turn on"}
      </Button>
    </div>
  )
}

/**
 * The optional live feed. Off by default and plainly priced; once on, each
 * month says whether the bank's own statement confirmed what the feed showed.
 */
function FeedRow({ onChange }: { onChange: () => void }) {
  const { data, refetch } = useApi(() => api.feed(), [])
  const [token, setToken] = React.useState("")
  const [asking, setAsking] = React.useState(false)
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  async function run(action: "connect" | "sync" | "disconnect") {
    setBusy(true)
    setError(null)
    try {
      await api.feedAction(action, action === "connect" ? token.trim() : undefined)
      setToken("")
      setAsking(false)
      refetch()
      onChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return null
  const synced = data.synced ? new Date(data.synced * 1000) : null
  return (
    <div className="px-5 py-3" data-write>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Radio className="size-4 shrink-0 text-[var(--color-ink-muted)]" aria-hidden />
        <div className="min-w-[14rem] flex-1 text-sm">
          {data.connected ? (
            <>
              <p>
                Live feed on: {data.accounts.length} account
                {data.accounts.length === 1 ? "" : "s"}
                {synced && `, updated ${synced.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`}.
              </p>
              <p className="text-xs text-[var(--color-ink-muted)]">
                New charges show up before the statement. When it arrives, the statement's
                rows replace them and confirm them.
              </p>
            </>
          ) : (
            <>
              <p>See this month's charges before the statement (optional)</p>
              <p className="text-xs text-[var(--color-ink-muted)]">
                Through{" "}
                <a
                  href="https://beta-bridge.simplefin.org/"
                  target="_blank"
                  rel="noreferrer"
                  className="underline underline-offset-2"
                >
                  SimpleFIN Bridge
                </a>
                , $1.50 a month paid to them. Link your banks there, then paste its setup
                token here. Your bank password never reaches this app.
              </p>
            </>
          )}
        </div>
        {data.connected ? (
          <div className="flex gap-1">
            <Button size="sm" variant="outline" onClick={() => void run("sync")} disabled={busy}>
              {busy && <Loader2 className="animate-spin" aria-hidden />}
              Update now
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void run("disconnect")} disabled={busy}>
              Turn off
            </Button>
          </div>
        ) : (
          !asking && (
            <Button size="sm" variant="outline" onClick={() => setAsking(true)}>
              Connect
            </Button>
          )
        )}
      </div>
      {asking && !data.connected && (
        <form
          className="mt-3 flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (token.trim()) void run("connect")
          }}
        >
          <Input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Setup token from SimpleFIN Bridge"
            aria-label="SimpleFIN setup token"
            className="h-8 min-w-[16rem] flex-1 font-mono text-xs"
            autoComplete="off"
          />
          <Button type="submit" size="sm" disabled={busy || !token.trim()}>
            {busy && <Loader2 className="animate-spin" aria-hidden />}
            Connect
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setAsking(false)}>
            Cancel
          </Button>
        </form>
      )}
      {data.checks.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-[var(--color-ink-secondary)]">
          {data.checks.slice(-4).map((c) => (
            <li key={c.statement} className="flex items-center gap-1.5">
              <CircleCheck
                className={cn(
                  "size-3.5",
                  c.matched === c.feedRows ? "text-[var(--color-good)]" : "text-[var(--color-warning)]",
                )}
                aria-hidden
              />
              {c.issuer} ····{c.last4}, statement closing {dateLabel(c.periodEnd)}: {c.matched} of{" "}
              {c.feedRows} feed rows confirmed
            </li>
          ))}
        </ul>
      )}
      {[...(error ? [error] : []), ...data.errors].map((e) => (
        <p key={e} className="mt-1 text-xs text-[var(--color-critical)]">
          {e}
        </p>
      ))}
    </div>
  )
}

function AccountRow({ a }: { a: SourceAccount }) {
  const [open, setOpen] = React.useState(false)
  const g = a.guide
  return (
    <li className="px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-[12rem] flex-1">
          <p className="text-sm font-medium">
            {a.issuer}
            {a.last4 && <span className="tnum text-[var(--color-ink-muted)]"> ····{a.last4}</span>}
          </p>
          <p className="text-xs text-[var(--color-ink-secondary)]">
            Latest statement closed {dateLabel(a.lastClosed)}
          </p>
        </div>
        {a.ready ? (
          <Badge variant="warning">
            <Download aria-hidden />
            {a.missing > 1 ? `About ${a.missing} ready` : "Next one ready"}
          </Badge>
        ) : (
          <Badge variant="neutral">
            <CircleCheck aria-hidden />
            Next around {dateLabel(a.expected)}
          </Badge>
        )}
        {g && (
          <div className="flex items-center gap-1">
            <Button size="sm" variant="ghost" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
              How
            </Button>
            <Button size="sm" variant="outline" asChild>
              <a href={g.url} target="_blank" rel="noreferrer">
                {g.bank}
                <ArrowUpRight aria-hidden />
              </a>
            </Button>
          </div>
        )}
      </div>
      {open && g && (
        <div className="mt-3 rounded-md bg-[var(--color-plane)] px-4 py-3 text-xs leading-relaxed">
          <ol className="list-decimal space-y-0.5 pl-4">
            {g.steps.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
          <dl className="mt-2 space-y-0.5 text-[var(--color-ink-secondary)]">
            {g.history && (
              <div>
                <dt className="inline font-medium text-[var(--color-ink)]">How far back: </dt>
                <dd className="inline">{g.history}</dd>
              </div>
            )}
            {g.bulk && (
              <div>
                <dt className="inline font-medium text-[var(--color-ink)]">Many at once: </dt>
                <dd className="inline">{g.bulk}</dd>
              </div>
            )}
            {g.exports.length > 0 && (
              <div>
                <dt className="inline font-medium text-[var(--color-ink)]">Formats: </dt>
                <dd className="inline">{g.exports.join(", ")}</dd>
              </div>
            )}
          </dl>
          {g.help && (
            <a
              href={g.help}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-flex items-center gap-0.5 underline underline-offset-2"
            >
              {g.bank}'s help page <ArrowUpRight className="size-3" aria-hidden />
            </a>
          )}
        </div>
      )}
    </li>
  )
}

/** On the overview: only when something is waiting, one line per account. */
export function ReadyReminder({ data }: { data: Sources }) {
  const ready = data.accounts.filter((a) => a.ready)
  if (ready.length === 0) return null
  return (
    <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3">
      <Download className="size-4 shrink-0 text-[var(--color-ink-muted)]" aria-hidden />
      <p className="min-w-[14rem] flex-1 text-sm">
        {ready.length === 1 ? "A new statement is" : `${ready.length} new statements are`}{" "}
        probably ready:{" "}
        {ready.map((a, i) => (
          <React.Fragment key={`${a.issuer}-${a.last4}`}>
            {i > 0 && ", "}
            {a.guide ? (
              <a
                href={a.guide.url}
                target="_blank"
                rel="noreferrer"
                className={cn("font-medium underline underline-offset-2")}
              >
                {a.issuer}
                {a.last4 && ` ····${a.last4}`}
              </a>
            ) : (
              <span className="font-medium">
                {a.issuer}
                {a.last4 && ` ····${a.last4}`}
              </span>
            )}
          </React.Fragment>
        ))}
        .{" "}
        {data.watch.on
          ? "Download them and they are added automatically."
          : "Download them, then drop them on the statements page."}
      </p>
      <Button size="sm" variant="ghost" asChild>
        <Link to="/statements">Statements</Link>
      </Button>
    </Card>
  )
}
