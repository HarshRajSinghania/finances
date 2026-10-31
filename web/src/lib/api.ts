/**
 * The one place that talks to api.py.
 *
 * No fetch calls live in components. Everything goes through here so errors,
 * query-string building and the base URL are handled once.
 */

import type {
  Anomalies,
  CategoryRow,
  Merchant,
  MonthRow,
  Recurring,
  Statement,
  Summary,
  TransactionPage,
  TransactionQuery,
  UploadResponse,
} from "./types"

/** Vite proxies /api to the Python server in dev; in production it serves this app. */
const BASE = "/api"

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v))
  }
  const url = qs.size ? `${BASE}${path}?${qs}` : `${BASE}${path}`

  let res: Response
  try {
    res = await fetch(url)
  } catch {
    // A dead socket is the common case in dev: the API is simply not running.
    // Say that, rather than surfacing "Failed to fetch".
    throw new ApiError(
      "Cannot reach the finances API. Start it with:  python api.py",
      0,
    )
  }

  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = ((await res.json()) as { error?: string }).error ?? detail
    } catch {
      /* body was not JSON; the status text will do */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

export const api = {
  summary: () => get<Summary>("/summary"),

  statements: () =>
    get<{ statements: Statement[]; coverage: Summary["coverage"] }>("/statements"),

  transactions: (q: TransactionQuery = {}) =>
    get<TransactionPage>("/transactions", q as Record<string, unknown>),

  categories: () => get<{ categories: CategoryRow[] }>("/categories"),

  months: () => get<{ months: MonthRow[] }>("/months"),

  recurring: (includeInterest = false) =>
    get<{ recurring: Recurring[]; committedMonthly: number }>("/recurring", {
      includeInterest,
    }),

  merchants: (limit = 50) => get<{ merchants: Merchant[] }>("/merchants", { limit }),

  anomalies: () => get<Anomalies>("/anomalies"),

  /**
   * Upload statement PDFs. They are parsed into the store immediately, so the
   * response already carries the new reconciliation verdict — the UI never has
   * to guess whether an upload was any good.
   */
  async upload(
    files: File[],
    onProgress?: (pct: number) => void,
  ): Promise<UploadResponse> {
    const body = new FormData()
    for (const f of files) body.append("file", f)

    // XHR rather than fetch: upload progress is the whole point of the
    // interaction, and fetch still cannot report it.
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open("POST", `${BASE}/upload`)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      }
      xhr.onload = () => {
        try {
          const parsed = JSON.parse(xhr.responseText) as UploadResponse & {
            error?: string
          }
          // 400 still carries per-file results worth showing, so it resolves.
          if (xhr.status >= 500) reject(new ApiError(parsed.error ?? "Upload failed", xhr.status))
          else resolve(parsed)
        } catch {
          reject(new ApiError("The server sent a response we could not read.", xhr.status))
        }
      }
      xhr.onerror = () =>
        reject(new ApiError("Cannot reach the finances API. Start it with:  python api.py", 0))
      xhr.send(body)
    })
  },

  async deleteStatement(sourceFile: string): Promise<void> {
    const res = await fetch(`${BASE}/statements/${encodeURIComponent(sourceFile)}`, {
      method: "DELETE",
    })
    if (!res.ok) throw new ApiError("Could not remove that statement.", res.status)
  },
}
