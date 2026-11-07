/**
 * The one place that talks to api.py.
 *
 * No fetch calls live in components. Everything goes through here so errors,
 * query-string building and the base URL are handled once.
 */

import type {
  Anomalies,
  AskReply,
  AskStatus,
  BudgetStatus,
  CategoryRow,
  ChatMessage,
  GoalInput,
  Goals,
  Merchant,
  MonthRow,
  Provider,
  Recurring,
  Statement,
  SubscriptionMark,
  SubscriptionReview,
  Summary,
  TransactionPage,
  TransactionQuery,
  UploadResponse,
  Wrapped,
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
      "Cannot reach the finances API. Start it with:  finances web",
      0,
    )
  }

  if (!res.ok) throw await failure(res)
  return (await res.json()) as T
}

/** Read the error a non-2xx response carries, falling back to the status text. */
async function failure(res: Response): Promise<ApiError> {
  let detail = res.statusText
  try {
    detail = ((await res.json()) as { error?: string }).error ?? detail
  } catch {
    /* body was not JSON; the status text will do */
  }
  return new ApiError(detail, res.status)
}

/**
 * A JSON write. The API only accepts application/json for these, which a page
 * on another origin cannot send without a preflight the server never answers.
 */
async function send<T>(method: "POST" | "DELETE", path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError("Cannot reach the finances API. Start it with:  finances web", 0)
  }
  if (!res.ok) throw await failure(res)
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

  wrapped: (year?: number) => get<Wrapped>("/wrapped", { year }),

  budgets: () => get<BudgetStatus>("/budgets"),

  /** Set a category's monthly limit; `null` removes it. */
  setBudget: (category: string, limit: number | null) =>
    send<BudgetStatus>("POST", "/budgets", { category, limit }),

  goals: () => get<Goals>("/goals"),

  saveGoal: (goal: GoalInput) => send<Goals>("POST", "/goals", goal),

  deleteGoal: (name: string) =>
    send<Goals>("DELETE", `/goals/${encodeURIComponent(name)}`),

  askStatus: () => get<AskStatus>("/ask"),

  /**
   * One question, answered by the user's own Claude or OpenAI key. The key
   * rides along with the request to the local server, which uses it for this
   * call and nothing else; an empty key means "use the server's own".
   */
  ask: (provider: Provider, model: string, key: string, messages: ChatMessage[]) =>
    send<AskReply>("POST", "/ask", { provider, model, key, messages }),

  subscriptionReview: () => get<SubscriptionReview>("/subscriptions/review"),

  /** Keep, cancelled, or `null` to undo either. */
  markSubscription: (key: string, status: SubscriptionMark) =>
    send<SubscriptionReview>("POST", "/subscriptions/mark", { key, status }),

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
        reject(new ApiError("Cannot reach the finances API. Start it with:  finances web", 0))
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
