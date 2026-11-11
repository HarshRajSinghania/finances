/**
 * The public demo: the dashboard with no server behind it.
 *
 * Built with VITE_DEMO=1 (`scripts/demo_site.py`), the app answers its own
 * /api requests from a snapshot of the made-up demo store, written next to it
 * as JSON. Transactions are filtered, sorted and paged here, the same way
 * api.py does it, so search and filters work. Anything that would change the
 * store is refused, and the page is read-only like the phone view.
 */

import type { Transaction } from "./types"

export const DEMO = import.meta.env.VITE_DEMO === "1"

const SNAPSHOT = `${import.meta.env.BASE_URL}demo-api/`
const NOT_HERE =
  "This is a read-only demo with made-up data. Install finances to use your own statements: uvx finances web"

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

let all: Promise<{ transactions: Transaction[]; nonSpend: string[] }> | null = null

/** api.py's Api.transactions, over the whole snapshot. */
async function transactions(q: URLSearchParams, real: typeof fetch): Promise<Response> {
  all ??= real(`${SNAPSHOT}transactions.json`).then((r) => r.json())
  const { transactions: every, nonSpend } = await all
  let rows = every
  const search = (q.get("search") ?? "").trim().toLowerCase()
  const has = (k: string) => (q.get(k) ?? "") !== ""
  if (q.get("spendOnly") === "true")
    rows = rows.filter((t) => t.amount > 0 && !nonSpend.includes(t.category))
  if (search)
    rows = rows.filter(
      (t) =>
        t.description.toLowerCase().includes(search) || t.merchant.toLowerCase().includes(search),
    )
  if (has("category")) rows = rows.filter((t) => t.category === q.get("category"))
  if (has("sourceFile")) rows = rows.filter((t) => t.sourceFile === q.get("sourceFile"))
  if (has("direction")) rows = rows.filter((t) => t.direction === q.get("direction"))
  if (has("start")) rows = rows.filter((t) => t.date >= q.get("start")!)
  if (has("end")) rows = rows.filter((t) => t.date <= q.get("end")!)

  const key: Record<string, (t: Transaction) => string | number> = {
    date: (t) => t.date,
    amount: (t) => t.amount,
    merchant: (t) => t.merchant.toLowerCase(),
    category: (t) => t.category,
  }
  const by = key[q.get("sort") ?? "date"] ?? key.date
  const desc = (q.get("order") ?? "desc") === "desc"
  rows = [...rows].sort((a, b) => {
    const [x, y] = desc ? [by(b), by(a)] : [by(a), by(b)]
    return x < y ? -1 : x > y ? 1 : 0
  })

  const limit = Math.max(1, Math.min(Number(q.get("limit") ?? 100) || 100, 1000))
  const offset = Math.max(0, Number(q.get("offset") ?? 0) || 0)
  const sum = rows.reduce((n, t) => n + t.amount, 0)
  return json({
    transactions: rows.slice(offset, offset + limit),
    total: rows.length,
    limit,
    offset,
    sumAmount: Math.round(sum * 100) / 100,
  })
}

/** Route every /api request to the snapshot. Called once, before the app renders. */
export function installDemo(): void {
  const real = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const href = input instanceof Request ? input.url : String(input)
    const url = new URL(href, window.location.href)
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/"))
      return real(input, init)

    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase()
    if (method !== "GET") return json({ error: NOT_HERE }, 403)

    const name = url.pathname.slice(5).replace(/\/+$/, "")
    const q = url.searchParams
    if (name === "session") return json({ readOnly: true, demo: true })
    if (name === "transactions") return transactions(q, real)

    let file = name.replaceAll("/", "__")
    if (name === "recurring" && q.get("includeInterest") === "true") file += "__interest"
    if (name === "wrapped" && q.get("year")) file += `__${q.get("year")}`
    const res = await real(`${SNAPSHOT}${file}.json`)
    if (!res.ok) return json({ error: NOT_HERE }, 404)
    if (name === "merchants" && q.get("limit")) {
      const body = (await res.json()) as { merchants: unknown[] }
      return json({ merchants: body.merchants.slice(0, Number(q.get("limit"))) })
    }
    return res
  }
}
