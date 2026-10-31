import { useCallback, useEffect, useRef, useState } from "react"

/**
 * Minimal data fetching.
 *
 * Deliberately not React Query: this app talks to a local process over
 * localhost, every response is small, and there is nothing to be gained from a
 * cache layer that has to be invalidated correctly. What it does need is the
 * three states kept honest — loading, error and data are separate, so a page
 * can never render a confident zero while a request is still in flight.
 */
export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  // Keeps a slow response from overwriting a newer, faster one.
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    setLoading(true)
    setError(null)
    fn()
      .then((d) => {
        if (mine === seq.current) setData(d)
      })
      .catch((e: unknown) => {
        if (mine === seq.current) setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const refetch = useCallback(() => setNonce((n) => n + 1), [])

  return { data, error, loading, refetch }
}

/** Debounce a fast-changing value, so a search box does not fire per keystroke. */
export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

export type Theme = "light" | "dark" | "system"

/** Theme, persisted, with the toggle winning over the OS in both directions. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("finances-theme") as Theme) || "system",
  )

  useEffect(() => {
    const root = document.documentElement
    if (theme === "system") root.removeAttribute("data-theme")
    else root.setAttribute("data-theme", theme)
    localStorage.setItem("finances-theme", theme)
  }, [theme])

  return { theme, setTheme }
}
