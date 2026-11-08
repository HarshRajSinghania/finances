import { Briefcase, FileSpreadsheet, Plus } from "lucide-react"
import * as React from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/misc"
import { useApi } from "@/hooks/useApi"

interface Clients {
  enabled: boolean
  current: string | null
  clients: { name: string; statements: number; reconciled: number; mismatched: number }[]
}

/**
 * For a practice (`finances web --clients FOLDER`): which client's books are
 * open, and a new one. Hidden entirely for one person's own finances.
 */
export function ClientSwitcher() {
  const { data } = useApi<Clients>(() => fetch("/api/clients").then((r) => r.json()), [])
  const [adding, setAdding] = React.useState(false)
  const [name, setName] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)

  if (!data?.enabled) return null

  async function open(client: string, create = false) {
    setError(null)
    const res = await fetch("/api/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: client, create }),
    })
    if (!res.ok) {
      setError((await res.json()).error ?? `HTTP ${res.status}`)
      return
    }
    // Every page's data is now another client's: start the app over.
    window.location.assign("/")
  }

  return (
    <div className="space-y-2 border-b border-[var(--color-hairline)] px-5 pb-4" data-write>
      <label className="flex items-center gap-1.5 text-xs text-[var(--color-ink-muted)]">
        <Briefcase className="size-3.5" aria-hidden /> Client
      </label>
      {data.clients.length > 0 && (
        <select
          value={data.current ?? ""}
          onChange={(e) => void open(e.target.value)}
          aria-label="Open a client's books"
          className="w-full rounded-md border border-[var(--color-hairline)] bg-[var(--color-surface)] px-2 py-1.5 text-sm"
        >
          {!data.current && <option value="">Choose a client</option>}
          {data.clients.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name}
              {c.statements ? ` (${c.reconciled}/${c.statements})` : ""}
            </option>
          ))}
        </select>
      )}
      {adding ? (
        <form
          className="flex gap-1"
          onSubmit={(e) => {
            e.preventDefault()
            if (name.trim()) void open(name.trim(), true)
          }}
        >
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Client name"
            aria-label="New client's name"
            className="h-8"
          />
          <Button type="submit" size="sm" disabled={!name.trim()}>
            Add
          </Button>
        </form>
      ) : (
        <div className="flex flex-wrap gap-1">
          <Button size="sm" variant="ghost" onClick={() => setAdding(true)}>
            <Plus aria-hidden /> New client
          </Button>
          {data.current && (
            <Button size="sm" variant="ghost" asChild>
              <a href="/api/workpapers.xlsx" download title="The reconciliation, to review and file">
                <FileSpreadsheet aria-hidden /> Workpapers
              </a>
            </Button>
          )}
        </div>
      )}
      {error && <p className="text-xs text-[var(--color-critical)]">{error}</p>}
    </div>
  )
}
