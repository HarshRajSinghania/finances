/**
 * Ask — questions about your money, answered by your own Claude or OpenAI key.
 *
 * The model never gets the database. It gets the same read-only tools the MCP
 * server offers and calls the ones it needs, so every number in an answer was
 * looked up, and the chips under each answer say which lookups it made.
 *
 * Answered questions are saved with the data (the store's settings table), so
 * the conversation is still here after leaving the page or restarting; the
 * trash button deletes it, after a confirmation.
 *
 * A local model (Ollama, LM Studio, llama.cpp, vLLM) needs no key, and the
 * question never leaves the machine; the server is found automatically or named.
 *
 * The key lives in this browser: in memory by default, in localStorage only if
 * "Remember on this device" is ticked. It goes to the local server with each
 * question, which passes it to the provider and keeps nothing.
 */

import { ArrowUp, Check, ChevronDown, Cpu, KeyRound, RefreshCw, MessageSquareText, ShieldAlert, ShieldCheck, ShieldQuestion, Trash2, Wrench, X } from "lucide-react"
import * as React from "react"

import { AnimatedTabs } from "@/components/easyui/AnimatedTabs"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/misc"
import { Spinner } from "@/components/ui/state"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import type { AskCheck, AskCitation, AskLocal, AskModel, AskStep, AskStatus, ChatMessage, Provider } from "@/lib/types"
import { cn } from "@/lib/utils"

const SUGGESTIONS = [
  "Where did my money go last month?",
  "Which subscriptions should I cancel?",
  "How much do I spend on eating out, month by month?",
  "What were my five biggest purchases this year?",
  "Am I spending more than I earn?",
]

/* ---------------------------------------------------------- settings */

// Every access is guarded: storage can be blocked, full, or absent entirely.
// Preferences and remembered keys go in localStorage; a key that is not to be
// remembered goes in sessionStorage, which the browser drops with the tab.
function storage(session = false) {
  const area = () => (session ? sessionStorage : localStorage)
  return {
    get(k: string) {
      try {
        return area().getItem(`finances.ask.${k}`) ?? ""
      } catch {
        return ""
      }
    },
    set(k: string, v: string) {
      try {
        if (v) area().setItem(`finances.ask.${k}`, v)
        else area().removeItem(`finances.ask.${k}`)
      } catch {
        /* not kept; it still works until the page is left */
      }
    },
  }
}
const store = storage()
const tab = storage(true)

interface Settings {
  provider: Provider
  model: Record<Provider, string>
  key: Record<Provider, string>
  remember: boolean
  endpoint: string                 // a local server named by hand; "" = find one
}

const PROVIDERS: Provider[] = ["anthropic", "openai", "local"]

function loadSettings(): Settings {
  const remembered = { anthropic: store.get("key.anthropic"), openai: store.get("key.openai") }
  const remember = Boolean(remembered.anthropic || remembered.openai)
  return {
    // A model on this machine first: nothing leaves it, and no key is needed.
    provider: (store.get("provider") as Provider) || "local",
    model: {
      anthropic: store.get("model.anthropic"),
      openai: store.get("model.openai"),
      local: store.get("model.local"),
    },
    key: {
      ...(remember ? remembered : { anthropic: tab.get("key.anthropic"), openai: tab.get("key.openai") }),
      local: "",
    },
    remember,
    endpoint: store.get("endpoint.local"),
  }
}

/* -------------------------------------------------------------- page */

interface Turn extends ChatMessage {
  steps?: AskStep[]
  check?: AskCheck
  model?: string
  error?: boolean
}

export function Ask() {
  const { data: status, error: statusError } = useApi(() => api.askStatus(), [])
  const [settings, setSettings] = React.useState<Settings>(loadSettings)
  const [turns, setTurns] = React.useState<Turn[]>([])
  const [draft, setDraft] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [confirming, setConfirming] = React.useState(false)
  const [historyError, setHistoryError] = React.useState<string | null>(null)
  const endRef = React.useRef<HTMLDivElement>(null)
  const follow = React.useRef(false)

  // The saved conversation, once, when the page opens. Not scrolled to: coming
  // back to the page should not jump it to the bottom.
  React.useEffect(() => {
    api
      .askHistory()
      .then((h) => setTurns((now) => (now.length ? now : h.turns)))
      .catch(() => {
        /* an older server keeps no history; the page still works */
      })
  }, [])

  const [local, setLocal] = React.useState<AskLocal | null>(null)
  const [finding, setFinding] = React.useState(false)
  const [localError, setLocalError] = React.useState<string | null>(null)

  const { provider } = settings
  const info = status?.providers[provider]
  const models = provider === "local" ? (local?.models ?? []) : (info?.models ?? [])
  const model = settings.model[provider] || models[0]?.id || ""
  const hasKey =
    provider === "local"
      ? Boolean(local?.found && local.models.length)
      : Boolean(settings.key[provider] || info?.serverKey)

  const findLocal = React.useCallback(async (endpoint: string) => {
    setFinding(true)
    setLocalError(null)
    try {
      setLocal(await api.askLocal(endpoint || undefined))
    } catch (e) {
      setLocal(null)
      setLocalError(e instanceof Error ? e.message : String(e))
    } finally {
      setFinding(false)
    }
  }, [])

  React.useEffect(() => {
    if (provider === "local" && !local && !finding && !localError) void findLocal(settings.endpoint)
  }, [provider]) // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    if (follow.current) endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [turns, busy])

  const clear = async () => {
    setHistoryError(null)
    try {
      await api.clearAskHistory()
      setTurns([])
      setConfirming(false)
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : String(e))
    }
  }

  const update = (next: Settings) => {
    setSettings(next)
    store.set("provider", next.provider)
    store.set("endpoint.local", next.endpoint)
    for (const p of PROVIDERS) {
      store.set(`model.${p}`, next.model[p])
      if (p === "local") continue
      store.set(`key.${p}`, next.remember ? next.key[p] : "")
      tab.set(`key.${p}`, next.remember ? "" : next.key[p])
    }
  }

  const ask = async (question: string) => {
    const q = question.trim()
    if (!q || busy || !hasKey) return
    const history: ChatMessage[] = [
      ...turns.filter((t) => !t.error).map(({ role, content }) => ({ role, content })),
      { role: "user", content: q },
    ]
    follow.current = true
    setConfirming(false)
    setTurns((t) => [...t, { role: "user", content: q }])
    setDraft("")
    setBusy(true)
    try {
      const r = await api.ask(provider, model, settings.key[provider], history,
                              provider === "local" ? local?.endpoint || settings.endpoint : undefined)
      setTurns((t) => [...t, { role: "assistant", content: r.answer, steps: r.steps, model: r.model, check: r.check }])
    } catch (e) {
      setTurns((t) => [
        ...t,
        { role: "assistant", content: e instanceof Error ? e.message : String(e), error: true },
      ])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <p className="max-w-2xl text-sm leading-relaxed text-[var(--color-ink-secondary)]">
        Ask anything about your spending in plain English. The assistant looks the
        numbers up in your store with read-only tools, the same ones{" "}
        <code className="font-mono text-xs">finances mcp</code> gives Claude Desktop,
        so it can read your data but never change it. Every amount in an answer is
        then checked against what those lookups returned: a charge links to the
        statement page it is printed on, and an amount that is not in your data is
        marked as such.
      </p>

      {statusError && (
        <p
          role="alert"
          className="flex gap-2 rounded-md border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/5 px-3.5 py-2.5 text-sm text-[var(--color-critical)]"
        >
          <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            {statusError.message === "no endpoint ask"
              ? "The server running this dashboard is older than the page and has no Ask support. Stop it and run "
              : `Could not load the model list (${statusError.message}). Try restarting `}
            <code className="font-mono text-xs">finances web</code> again.
          </span>
        </p>
      )}
      <KeySettings
        status={status}
        settings={settings}
        onChange={update}
        model={model}
        models={models}
        local={{ found: local, finding, error: localError, find: findLocal }}
      />

      <Card className="flex min-h-[28rem] flex-col">
        <div className="flex-1 space-y-5 p-5" aria-live="polite">
          {turns.length === 0 ? (
            <EmptyChat disabled={!hasKey} onPick={ask} />
          ) : (
            turns.map((t, i) => <Bubble key={i} turn={t} />)
          )}
          {busy && (
            <div className="flex items-center gap-2 text-xs text-[var(--color-ink-muted)]">
              <Spinner className="size-3.5" />
              {provider === "local"
                ? "Looking it up… a local model can take a minute"
                : "Looking it up…"}
            </div>
          )}
          <div ref={endRef} />
        </div>

        {(confirming || historyError) && (
          <div
            role="alertdialog"
            aria-label="Delete the conversation"
            className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-[var(--color-hairline)] bg-[var(--color-critical)]/5 px-4 py-2.5 text-sm"
          >
            <span className="flex-1 text-[var(--color-ink-secondary)]">
              {historyError
                ? `Could not delete it: ${historyError}`
                : `Delete all ${turns.length} messages? They are removed from this computer and cannot be brought back.`}
            </span>
            <Button size="sm" variant="danger" onClick={() => void clear()}>
              <Trash2 /> Delete
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setConfirming(false)
                setHistoryError(null)
              }}
            >
              Keep it
            </Button>
          </div>
        )}

        <form
          className="flex items-end gap-2 border-t border-[var(--color-hairline)] p-3"
          onSubmit={(e) => {
            e.preventDefault()
            void ask(draft)
          }}
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                void ask(draft)
              }
            }}
            rows={1}
            disabled={!hasKey}
            placeholder={hasKey ? "Ask about your money…" : "Add an API key above to start"}
            aria-label="Your question"
            className="max-h-40 min-h-9 flex-1 resize-none rounded-md border border-[var(--color-hairline)] bg-[var(--color-surface)] px-3 py-2 text-sm placeholder:text-[var(--color-ink-muted)] disabled:opacity-50 field-sizing-content"
          />
          {turns.length > 0 && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => setConfirming(true)}
              disabled={busy || confirming}
              aria-label="Delete the conversation"
              title="Delete the conversation"
            >
              <Trash2 />
            </Button>
          )}
          <Button
            type="submit"
            size="icon"
            disabled={busy || !hasKey || !draft.trim()}
            aria-label="Ask"
          >
            <ArrowUp />
          </Button>
        </form>
      </Card>
    </div>
  )
}

/* --------------------------------------------------------- key card */

interface LocalState {
  found: AskLocal | null
  finding: boolean
  error: string | null
  find: (endpoint: string) => Promise<void>
}

function KeySettings({
  status,
  settings,
  onChange,
  model,
  models,
  local,
}: {
  status: AskStatus | null
  settings: Settings
  onChange: (s: Settings) => void
  model: string
  models: AskModel[]
  local: LocalState
}) {
  const { provider } = settings
  const info = status?.providers[provider]
  const saved = settings.key[provider]
  const [editing, setEditing] = React.useState(!saved && !info?.serverKey)
  const [draft, setDraft] = React.useState("")
  const label = info?.label ?? { anthropic: "Claude", openai: "OpenAI", local: "Local model" }[provider]
  const isLocal = provider === "local"

  React.useEffect(() => {
    setEditing(!settings.key[provider] && !info?.serverKey)
    setDraft("")
  }, [provider, info?.serverKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const saveKey = () => {
    if (!draft.trim()) return
    onChange({ ...settings, key: { ...settings.key, [provider]: draft.trim() } })
    setDraft("")
    setEditing(false)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="size-4" strokeWidth={1.75} aria-hidden />
          Your AI provider
        </CardTitle>
        <CardDescription>
          Bring your own key, or run a model on this computer. Nothing goes through
          anyone else.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <AnimatedTabs
          renderContent={false}
          variant="pill"
          activeTab={provider}
          onChange={(id) => onChange({ ...settings, provider: id as Provider })}
          tabs={[
            { id: "anthropic", label: "Claude" },
            { id: "openai", label: "OpenAI" },
            { id: "local", label: "Local model" },
          ]}
        />

        <div className="grid gap-4 sm:grid-cols-[minmax(0,16rem)_1fr]">
          <ModelPicker
            models={models}
            source={
              isLocal
                ? local.found?.found
                  ? `served by ${local.found.server}`
                  : undefined
                : status?.checked && `from the docs, ${status.checked}`
            }
            value={model}
            onChange={(id) => onChange({ ...settings, model: { ...settings.model, [provider]: id } })}
          />

          {isLocal ? (
            <LocalServer settings={settings} onChange={onChange} local={local} />
          ) : (
          <div className="space-y-1.5">
            <span className="text-xs font-medium text-[var(--color-ink-secondary)]">
              {label} API key
            </span>
            {editing ? (
              <form
                className="flex gap-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  saveKey()
                }}
              >
                <Input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder={provider === "anthropic" ? "sk-ant-…" : "sk-…"}
                  aria-label={`${label} API key`}
                  className="font-mono"
                />
                <Button type="submit" disabled={!draft.trim()}>
                  <Check /> Use key
                </Button>
                {(saved || info?.serverKey) && (
                  <Button type="button" variant="ghost" size="icon" onClick={() => setEditing(false)} aria-label="Cancel">
                    <X />
                  </Button>
                )}
              </form>
            ) : (
              <div className="flex h-9 items-center gap-2 text-sm">
                <span className="inline-flex items-center gap-1.5 text-[var(--color-good)]">
                  <Check className="size-4" />
                  {saved ? (
                    <span className="font-mono text-xs">…{saved.slice(-4)}</span>
                  ) : (
                    "Using the server's key"
                  )}
                </span>
                <Button variant="link" size="sm" className="px-1" onClick={() => setEditing(true)}>
                  Change
                </Button>
                {saved && (
                  <Button
                    variant="link"
                    size="sm"
                    className="px-1 text-[var(--color-critical)]"
                    onClick={() => {
                      onChange({ ...settings, key: { ...settings.key, [provider]: "" } })
                      setEditing(!info?.serverKey)
                    }}
                  >
                    Forget
                  </Button>
                )}
              </div>
            )}
          </div>
          )}
        </div>

        {!isLocal && (
        <label className="flex items-center gap-2 text-xs text-[var(--color-ink-secondary)]">
          <input
            type="checkbox"
            checked={settings.remember}
            onChange={(e) => onChange({ ...settings, remember: e.target.checked })}
            className="size-3.5 accent-[var(--color-series-1)]"
          />
          Remember keys on this device (kept in this browser only; otherwise
          they are forgotten when you close the tab)
        </label>
        )}

        <p className="flex gap-2 rounded-md bg-[var(--color-plane)] px-3 py-2.5 text-xs leading-relaxed text-[var(--color-ink-secondary)]">
          {isLocal ? (
            <>
              <Cpu className="mt-0.5 size-3.5 shrink-0 text-[var(--color-good)]" aria-hidden />
              <span>
                Everything stays on your machine: the question and the numbers the
                model looks up go only to{" "}
                {local.found?.found ? (
                  <>
                    {local.found.server} at{" "}
                    <span className="font-mono">{local.found.endpoint}</span>
                  </>
                ) : (
                  "the local server"
                )}
                . A model that cannot use tools is handed the main figures instead.
              </span>
            </>
          ) : (
            <>
              <ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-[var(--color-warning)]" aria-hidden />
              <span>
                Your question and the numbers the assistant looks up (totals, merchant
                names, amounts) are sent to {provider === "anthropic" ? "Anthropic" : "OpenAI"}{" "}
                to write the answer. Your statement files and database stay here.
                {model && <> Model: <span className="font-mono">{model}</span>.</>}
              </span>
            </>
          )}
        </p>
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------ local */

/** Which local server: found automatically, or an address typed in. */
function LocalServer({
  settings,
  onChange,
  local,
}: {
  settings: Settings
  onChange: (s: Settings) => void
  local: LocalState
}) {
  const [draft, setDraft] = React.useState(settings.endpoint)
  const f = local.found
  const look = (endpoint: string) => {
    onChange({ ...settings, endpoint })
    void local.find(endpoint)
  }
  return (
    <div className="space-y-1.5">
      <label htmlFor="ask-endpoint" className="text-xs font-medium text-[var(--color-ink-secondary)]">
        Server
      </label>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          look(draft.trim())
        }}
      >
        <Input
          id="ask-endpoint"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={f?.found ? f.endpoint : "found automatically, or http://localhost:11434/v1"}
          className="font-mono"
        />
        <Button type="submit" variant="outline" disabled={local.finding}>
          <RefreshCw className={cn(local.finding && "animate-spin")} />
          {draft.trim() ? "Connect" : "Find"}
        </Button>
      </form>
      <p className="text-xs" role="status">
        {local.finding ? (
          <span className="text-[var(--color-ink-muted)]">Looking for a model server…</span>
        ) : local.error ? (
          <span className="text-[var(--color-critical)]">{local.error}</span>
        ) : f?.found ? (
          <span className="inline-flex items-center gap-1.5 text-[var(--color-good)]">
            <Check className="size-3.5" />
            {f.server} · {f.models.length} chat model{f.models.length === 1 ? "" : "s"}
            {!f.models.length && " (pull one first, e.g. ollama pull qwen3.5)"}
          </span>
        ) : f ? (
          <span className="text-[var(--color-ink-secondary)]">
            No model server found. Start Ollama (<code className="font-mono">ollama serve</code>)
            or LM Studio&apos;s local server, then press Find.
          </span>
        ) : null}
      </p>
    </div>
  )
}

/* ------------------------------------------------------------ model */

const OTHER = "__other__"

/**
 * The provider's current models, from its own docs, in a dropdown. "Other"
 * opens a text field for any model ID the list does not have yet, such as a
 * release newer than this version of finances.
 */
function ModelPicker({
  models,
  source,
  value,
  onChange,
}: {
  models: AskModel[]
  source?: string | false
  value: string
  onChange: (id: string) => void
}) {
  const known = models.some((m) => m.id === value)
  const [custom, setCustom] = React.useState(!known && Boolean(value))
  React.useEffect(() => setCustom(!known && Boolean(value)), [models]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-1.5">
      <label htmlFor="ask-model" className="text-xs font-medium text-[var(--color-ink-secondary)]">
        Model
      </label>
      <div className="relative">
        <select
          id="ask-model"
          value={custom ? OTHER : value}
          onChange={(e) => {
            if (e.target.value === OTHER) {
              setCustom(true)
              return
            }
            setCustom(false)
            onChange(e.target.value)
          }}
          className="h-9 w-full appearance-none rounded-md border border-[var(--color-hairline)] bg-[var(--color-surface)] pl-3 pr-8 text-sm"
        >
          {models.map((m, i) => (
            <option key={m.id} value={m.id}>
              {m.name}
              {i === 0 ? " (default)" : ""}
            </option>
          ))}
          <option value={OTHER}>Other model ID…</option>
        </select>
        <ChevronDown
          className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-[var(--color-ink-muted)]"
          aria-hidden
        />
      </div>
      {custom ? (
        <Input
          autoFocus
          spellCheck={false}
          value={known ? "" : value}
          placeholder="e.g. a model released after this list"
          aria-label="Model ID"
          className="font-mono text-xs"
          onChange={(e) => onChange(e.target.value.trim())}
        />
      ) : (
        <p className="font-mono text-[11px] text-[var(--color-ink-muted)]">
          {value}
          {source && <span className="font-sans"> · {source}</span>}
        </p>
      )}
    </div>
  )
}

/* ------------------------------------------------------ conversation */

function EmptyChat({ disabled, onPick }: { disabled: boolean; onPick: (q: string) => void }) {
  return (
    <div className="flex flex-col items-center gap-4 py-10 text-center">
      <MessageSquareText className="size-7 text-[var(--color-ink-muted)]" strokeWidth={1.5} />
      <div className="space-y-1">
        <p className="text-sm font-medium">Ask about your money</p>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          {disabled ? "Add an API key above, then try one of these." : "Try one of these, or write your own."}
        </p>
      </div>
      <div className="flex max-w-xl flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            disabled={disabled}
            onClick={() => onPick(s)}
            className="rounded-full border border-[var(--color-hairline)] bg-[var(--color-surface)] px-3 py-1.5 text-xs text-[var(--color-ink-secondary)] transition-colors hover:bg-[var(--color-plane)] hover:text-[var(--color-ink)] disabled:pointer-events-none disabled:opacity-50"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

const TOOL_LABEL: Record<string, string> = {
  summary: "summary",
  spending_by_category: "spending by category",
  monthly_totals: "monthly totals",
  search_transactions: "transaction search",
  subscriptions: "subscriptions",
  cancel_list: "cancel list",
  cashflow_forecast: "cash-flow forecast",
  check: "reconciliation check",
  anomalies: "anomalies",
  budgets: "budgets",
  wrapped: "year in review",
  query: "SQL query",
}

function Bubble({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-[var(--color-ink)] px-3.5 py-2 text-sm text-[var(--color-surface)]">
          {turn.content}
        </p>
      </div>
    )
  }
  if (turn.error) {
    return (
      <p className="max-w-[85%] rounded-md border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/5 px-3.5 py-2 text-sm text-[var(--color-critical)]">
        {turn.content}
      </p>
    )
  }
  const tools = [...new Set((turn.steps ?? []).map((s) => s.tool))]
  return (
    <div className="max-w-[85%] space-y-2">
      <Answer text={turn.content} citations={turn.check?.citations} />
      {turn.check && <Verdict verdict={turn.check.verdict} />}
      {tools.length > 0 && (
        <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--color-ink-muted)]">
          <Wrench className="size-3" aria-hidden />
          Looked up:
          {tools.map((t) => (
            <span
              key={t}
              className="rounded border border-[var(--color-hairline)] bg-[var(--color-plane)] px-1.5 py-0.5"
            >
              {TOOL_LABEL[t] ?? t}
            </span>
          ))}
          {turn.model && <span className="ml-1">· {turn.model}</span>}
        </p>
      )}
    </div>
  )
}

/**
 * The little markdown the prompt allows — paragraphs, "- " and "1. " lists,
 * **bold** and `code` — rendered as elements, never as HTML, so nothing a
 * model writes can run in this page.
 */
function Answer({ text, citations }: { text: string; citations?: AskCitation[] }) {
  // The checker found the amounts in the same order this walks the text, so
  // each one takes the next citation whose text it matches.
  const queue = [...(citations ?? [])]
  const inline = (s: string) => renderInline(s, queue)
  const blocks: React.ReactNode[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  const flush = () => {
    if (!list) return
    const Tag = list.ordered ? "ol" : "ul"
    blocks.push(
      <Tag
        key={blocks.length}
        className={cn("space-y-1 pl-5", list.ordered ? "list-decimal" : "list-disc")}
      >
        {list.items.map((it, i) => (
          <li key={i}>{inline(it)}</li>
        ))}
      </Tag>,
    )
    list = null
  }
  for (const raw of text.split("\n")) {
    const line = raw.trim()
    const bullet = /^[-*•]\s+(.*)$/.exec(line)
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      const ordered = Boolean(numbered)
      if (list && list.ordered !== ordered) flush()
      list ??= { ordered, items: [] }
      list.items.push((bullet ?? numbered)![1])
      continue
    }
    flush()
    if (line) blocks.push(<p key={blocks.length}>{inline(line.replace(/^#+\s*/, ""))}</p>)
  }
  flush()
  return <div className="space-y-2 text-sm leading-relaxed">{blocks}</div>
}

function renderInline(s: string, queue: AskCitation[]): React.ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4)
      return <strong key={i} className="font-semibold">{cited(part.slice(2, -2), queue)}</strong>
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2)
      return <code key={i} className="rounded bg-[var(--color-plane)] px-1 font-mono text-xs">{part.slice(1, -1)}</code>
    return <React.Fragment key={i}>{cited(part, queue)}</React.Fragment>
  })
}

// The same amounts finances/cite.py looks for.
const MONEY = /([-−])?\s?([$€£])\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s?[kK]\b)?/g

function cited(s: string, queue: AskCitation[]): React.ReactNode {
  if (!queue.length) return s
  const out: React.ReactNode[] = []
  let last = 0
  for (const m of s.matchAll(MONEY)) {
    const at = queue.findIndex((c) => c.text === m[0].trim())
    if (at < 0) continue
    const [c] = queue.splice(at, 1)
    const start = m.index! + (m[0].length - m[0].trimStart().length)
    out.push(s.slice(last, start), <Cited key={start} c={c} text={m[0].trim()} />)
    last = m.index! + m[0].length
  }
  out.push(s.slice(last))
  return out
}

/** One amount, underlined by where it came from; a charge opens its statement page. */
function Cited({ c, text }: { c: AskCitation; text: string }) {
  const src = c.source
  if (c.status === "missing" || !src) {
    return (
      <span
        title="Not in your data: the model wrote this amount itself."
        className="underline decoration-[var(--color-critical)] decoration-wavy underline-offset-4"
      >
        {text}
      </span>
    )
  }
  const lookup = TOOL_LABEL[src.tool] ?? src.tool
  if (src.kind === "row" && src.file) {
    const where = src.page ? `, page ${src.page}` : ""
    return (
      <a
        href={api.originalUrl(src.file) + (src.page ? `#page=${src.page}` : "")}
        target="_blank"
        rel="noreferrer"
        title={`${src.merchant ?? "Charge"} on ${src.date}: ${src.file}${where}`}
        className="underline decoration-[var(--color-good)] decoration-dotted underline-offset-4 hover:decoration-solid"
      >
        {text}
      </a>
    )
  }
  const title =
    src.kind === "derived" && src.of
      ? `The ${src.op} of ${src.of[0].toFixed(2)} and ${src.of[1].toFixed(2)}, from ${lookup}`
      : `From ${lookup}`
  return (
    <span
      title={title}
      className="underline decoration-[var(--color-good)] decoration-dotted underline-offset-4"
    >
      {text}
    </span>
  )
}

const VERDICT = {
  verified: { icon: ShieldCheck, label: "Verified", tone: "text-[var(--color-good)]" },
  found: { icon: ShieldQuestion, label: "In your data", tone: "text-[var(--color-ink-secondary)]" },
  unverified: { icon: ShieldAlert, label: "Unverified", tone: "text-[var(--color-critical)]" },
} as const

function Verdict({ verdict }: { verdict: AskCheck["verdict"] }) {
  if (verdict.level === "none") return null
  const v = VERDICT[verdict.level]
  return (
    <p className={cn("flex items-start gap-1.5 text-xs", v.tone)}>
      <v.icon className="mt-px size-3.5 shrink-0" aria-hidden />
      <span>
        <span className="font-medium">{v.label}.</span>{" "}
        <span className="text-[var(--color-ink-secondary)]">{verdict.text}</span>
      </span>
    </p>
  )
}
