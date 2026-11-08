import { useEffect, useState } from "react"

/**
 * Whether this page is the read-only phone view.
 *
 * Asked once per page load. While read-only, the root element carries the
 * `read-only` class, and every control marked `data-write` is hidden by CSS:
 * the server refuses those requests anyway, so offering them would only lead
 * to an error.
 */
let session: Promise<boolean> | null = null

function readOnly(): Promise<boolean> {
  session ??= fetch("/api/session")
    .then((r) => (r.ok ? r.json() : { readOnly: false }))
    .then((s: { readOnly?: boolean }) => {
      const ro = Boolean(s.readOnly)
      document.documentElement.classList.toggle("read-only", ro)
      return ro
    })
    .catch(() => false)
  return session
}

export function useReadOnly(): boolean {
  const [ro, setRo] = useState(false)
  useEffect(() => {
    let live = true
    void readOnly().then((v) => live && setRo(v))
    return () => {
      live = false
    }
  }, [])
  return ro
}
