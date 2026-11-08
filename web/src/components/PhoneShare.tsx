import { Loader2, Smartphone, X } from "lucide-react"
import QRCode from "qrcode"
import * as React from "react"

import { Button } from "@/components/ui/button"

interface PhoneState {
  on: boolean
  url: string
}

async function call(on?: boolean): Promise<PhoneState> {
  const res = await fetch("/api/phone", on === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on }),
  })
  const body = await res.json()
  if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`)
  return body
}

/**
 * Look at the dashboard on a phone, over the home Wi-Fi.
 *
 * Off until asked for. On, it shows a code to scan: the link carries a fresh
 * key, the phone gets a read-only view, and turning it off (or stopping the
 * dashboard) ends it.
 */
export function PhoneShare() {
  const [open, setOpen] = React.useState(false)
  const [state, setState] = React.useState<PhoneState | null>(null)
  const [qr, setQr] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (open && !state) call().then(setState).catch((e: Error) => setError(e.message))
  }, [open, state])

  React.useEffect(() => {
    if (!state?.url) return setQr("")
    QRCode.toDataURL(state.url, { margin: 1, width: 220 }).then(setQr).catch(() => setQr(""))
  }, [state?.url])

  async function toggle() {
    setBusy(true)
    setError(null)
    try {
      setState(await call(!state?.on))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative" data-write>
      <Button
        variant="ghost"
        size="icon"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Look at this on your phone"
        title="Look at this on your phone"
      >
        <Smartphone />
      </Button>
      {open && (
        <div
          role="dialog"
          aria-label="Phone view"
          className="absolute right-0 z-50 mt-2 w-72 rounded-[var(--radius-card)] border border-[var(--color-hairline)] bg-[var(--color-surface)] p-4 text-sm shadow-lg"
        >
          <div className="flex items-start justify-between gap-2">
            <p className="font-medium">On your phone</p>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close"
              className="text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]"
            >
              <X className="size-4" />
            </button>
          </div>
          {state?.on ? (
            <>
              <p className="mt-1 text-xs text-[var(--color-ink-secondary)]">
                Scan this with your phone's camera, on the same Wi-Fi. It opens a
                read-only view; anyone with the code can see your numbers until you turn
                it off.
              </p>
              {qr && (
                <img src={qr} alt="Code to scan with your phone" className="mx-auto mt-3 size-44 rounded" />
              )}
              <p className="mt-2 font-mono text-[10px] break-all text-[var(--color-ink-muted)]">
                {state.url}
              </p>
            </>
          ) : (
            <p className="mt-1 text-xs text-[var(--color-ink-secondary)]">
              See this dashboard on your phone or a partner's, read-only, over your home
              Wi-Fi. Nothing goes through the internet. Use it only on a network you trust.
            </p>
          )}
          {error && <p className="mt-2 text-xs text-[var(--color-critical)]">{error}</p>}
          <Button
            size="sm"
            variant={state?.on ? "outline" : "default"}
            className="mt-3 w-full"
            onClick={() => void toggle()}
            disabled={busy || !state}
          >
            {busy && <Loader2 className="animate-spin" aria-hidden />}
            {state?.on ? "Turn off" : "Turn on"}
          </Button>
        </div>
      )}
    </div>
  )
}
