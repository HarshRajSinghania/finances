/**
 * The shape of everything api.py returns.
 *
 * Kept in one file and imported everywhere, so a field rename shows up as a
 * type error rather than as an empty cell on a page nobody opened.
 */

/** Whether a statement's extracted charges agree with its own printed totals. */
export type ReconcileStatus = "ok" | "mismatch" | "unchecked"

export interface CoverageGap {
  account: string
  after: string
  before: string
  days: number
}

export interface CoverageMismatch {
  sourceFile: string
  issuer: string
  diff: number | null
  note: string
}

/**
 * The reconciliation verdict for the whole store. This is the project's
 * headline claim, so it is never reduced to a boolean: "nothing was checked"
 * and "the numbers disagree" are different answers and the UI shows them
 * differently.
 */
export interface Coverage {
  statements: number
  reconciled: number
  mismatched: number
  unchecked: number
  pct: number
  trusted: boolean
  gaps: CoverageGap[]
  mismatches: CoverageMismatch[]
}

export interface Account {
  issuer: string
  last4: string
}

export interface Summary {
  transactions: number
  spendTransactions: number
  totalSpend: number
  totalCredits: number
  monthlyAverage: number
  essentialMonthly: number
  committedMonthly: number
  incomeAverage: number | null
  surplus: number | null
  firstDate: string | null
  lastDate: string | null
  monthsCovered: number
  completeMonths: string[]
  subscriptionCount: number
  coverage: Coverage
  accounts: Account[]
}

export interface Statement {
  sourceFile: string
  issuer: string
  accountLast4: string
  period: string
  periodStart: string | null
  periodEnd: string | null
  previousBalance: number | null
  newBalance: number | null
  purchases: number | null
  payments: number | null
  interestCharged: number | null
  feesCharged: number | null
  creditLimit: number | null
  purchaseApr: number | null
  chargesExtracted: number | null
  creditsExtracted: number | null
  reconciled: 0 | 1 | null
  reconcileDiff: number | null
  reconcileNote: string
  status: ReconcileStatus
  transactionCount: number
}

export interface Transaction {
  date: string
  postDate: string | null
  description: string
  merchant: string
  merchantKey: string
  /** Money out is positive, money in is negative. */
  amount: number
  direction: "out" | "in"
  category: string
  issuer: string
  accountLast4: string
  sourceFile: string
  seq: number
}

export interface CategoryRow {
  category: string
  total: number
  perMonth: number
  share: number
  count: number
}

export interface MonthRow {
  month: string
  spend: number
  income: number
  net: number
  /** False when the data covers only part of the month — excluded from averages. */
  complete: boolean
}

export type Cadence =
  | "weekly"
  | "biweekly"
  | "monthly"
  | "quarterly"
  | "semiannual"
  | "annual"

export interface Recurring {
  key: string
  name: string
  category: string
  cadence: Cadence
  amount: number
  n: number
  perMonth: number
  perYear: number
  firstSeen: string
  lastSeen: string
  dayOfMonth: number
  nextExpected: string | null
  priceChange: number
  pricePct: number
  /** 0..1 — how steady the cadence and the price are. */
  confidence: number
  /** The next charge is well overdue: cancelled, or a card that expired. */
  isStale: boolean
}

export interface Merchant {
  key: string
  name: string
  category: string
  total: number
  n: number
  avg: number
  last: string
}

export interface Anomalies {
  duplicates: { name: string; amount: number; date: string; n: number }[]
  outliers: {
    name: string
    date: string
    amount: number
    typical: number
    category: string
  }[]
  largest: Transaction[]
}

export interface TransactionQuery {
  search?: string
  category?: string
  sourceFile?: string
  direction?: "out" | "in"
  start?: string
  end?: string
  spendOnly?: boolean
  sort?: "date" | "amount" | "merchant" | "category"
  order?: "asc" | "desc"
  limit?: number
  offset?: number
}

export interface TransactionPage {
  transactions: Transaction[]
  total: number
  limit: number
  offset: number
  sumAmount: number
}

export interface UploadResult {
  ok: boolean
  file: string
  replaced?: boolean
  error?: string
  log?: string
  statement?: Statement | null
  coverage?: Coverage
  transactionCount?: number
}

export interface UploadResponse {
  results: UploadResult[]
  uploaded: number
}
