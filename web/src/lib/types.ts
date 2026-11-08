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
  /** ISO 4217 code every total is in. */
  currency: string
  /** Rows held out of the totals because their currency has no rate set. */
  fxMissing: Record<string, number>
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
  /** pdf, csv, ofx, … or an app export: mint, ynab, monarch, tiller, copilot. */
  sourceFormat: string
  /** "card", "bank", "investment", or "" when the statement does not say. */
  accountType: string
  /** When the card's payment is due (ISO), null when the statement does not say. */
  paymentDue: string | null
  transactionCount: number
  /** Whether the file it was read from is still in the statement folder. */
  hasFile?: boolean
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
  /** The statement page it was read from; 0 when not recorded. */
  page?: number
  /** What was bought, from an order history or receipt matched to this charge. */
  items?: { name: string; quantity: number | null; amount: number | null }[]
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
  /** An order history or receipts file: items attached to charges. */
  lineItems?: { file: string; kind: string; groups: number; matched: number; items: number }
  /** Accounts the file held; an app export or OFX can carry several. */
  accounts?: number
}

export interface UploadResponse {
  results: UploadResult[]
  uploaded: number
}

/* ------------------------------------------------------------ wrapped */

export interface Wrapped {
  year: number | null
  years: number[]
  empty: boolean
  months?: string[]
  fullYear?: boolean
  total?: number
  transactions?: number
  perDay?: number
  perMonth?: number
  income?: number | null
  kept?: number | null
  topCategories?: { category: string; total: number; share: number; count: number }[]
  topMerchants?: { name: string; total: number; visits: number }[]
  favourite?: { name: string; visits: number; total: number }
  biggest?: { name: string; amount: number; date: string; category: string }
  biggestDay?: { date: string; total: number }
  biggestMonth?: { month: string; total: number }
  quietestMonth?: { month: string; total: number }
  byMonth?: { month: string; total: number }[]
  byDay?: { date: string; total: number }[]
  weekday?: string | null
  newMerchants?: number
  merchants?: number
  subscriptions?: { count: number; paid: number; perYear: number }
  vsLastYear?: { months: number; now: number; then: number; pct: number } | null
  /** Share of the year's purchases that come from a statement that reconciles. */
  traced?: number
}

/* ------------------------------------------------------------ budgets */

export type BudgetState = "over" | "pace" | "ok"

export interface BudgetRow {
  category: string
  limit: number
  spent: number
  projected: number
  lastMonth: number
  average: number
  left: number
  state: BudgetState
  pct: number
}

export interface BudgetStatus {
  month: string | null
  asOf?: string
  inProgress?: boolean
  daysElapsed?: number
  daysInMonth?: number
  budgets: BudgetRow[]
  suggestions: { category: string; average: number; suggested: number }[]
  totalLimit?: number
  totalSpent?: number
}

/* -------------------------------------------------------------- goals */

export interface Goal {
  name: string
  target: number
  saved: number
  by: string | null
  priority: number
  monthly: number | null
  note: string | null
  remaining: number
  allocated: number
  required: number | null
  eta: string | null
  late: boolean
  pct: number
}

export interface Goals {
  surplus: number | null
  surplusSource: "plan" | "statements" | null
  committedMonthly: number
  unallocated: number | null
  goals: Goal[]
}

export interface GoalInput {
  name: string
  target?: number
  saved?: number
  by?: string | null
  priority?: number
  monthly?: number
}

/* -------------------------------------------------------- cancel list */

export interface ReviewItem extends Recurring {
  reasons: string[]
  markedAt: string | null
}

export type SubscriptionMark = "keep" | "cancelled" | null

export interface SubscriptionReview {
  candidates: ReviewItem[]
  kept: ReviewItem[]
  cancelled: ReviewItem[]
  stillCharging: ReviewItem[]
  gone: ReviewItem[]
  savingsPerYear: number
  savedPerYear: number
}

/* ---------------------------------------------------------------- ask */

export type Provider = "anthropic" | "openai" | "local"

export interface AskModel {
  id: string
  name: string
}

export interface AskStatus {
  /** When the model lists were last checked against the providers' docs. */
  checked: string
  providers: Record<Provider, { label: string; models: AskModel[]; serverKey: boolean }>
}

export interface ChatMessage {
  role: "user" | "assistant"
  content: string
}

export interface AskStep {
  tool: string
  input: Record<string, unknown>
  ok: boolean
}

/** Where one amount in an answer came from, or that it came from nowhere. */
export interface AskCitation {
  text: string
  value: number
  status: "found" | "missing"
  source?: {
    kind: "row" | "lookup" | "derived"
    tool: string
    step: number
    file?: string
    page?: number
    date?: string
    merchant?: string
    op?: "sum" | "difference"
    of?: [number, number]
  }
}

export interface AskCheck {
  citations: AskCitation[]
  verdict: {
    level: "verified" | "found" | "unverified" | "none"
    text: string
    amounts: number
    missing: number
  }
}

export interface AskReply {
  answer: string
  check?: AskCheck
  steps: AskStep[]
  provider: Provider
  model: string
  usage: { input: number; output: number }
}

/** Saved turns. Assistant turns carry the lookups behind them. */
export interface AskHistory {
  turns: (ChatMessage & { at?: string; steps?: AskStep[]; model?: string; check?: AskCheck })[]
}

/** A model server on this machine or network, and the chat models it serves. */
export interface AskLocal {
  found: boolean
  endpoint: string
  server: string
  models: AskModel[]
}

/* ------------------------------------------------------------ payments */

export type PaymentState = "due" | "paid" | "minimum paid" | "unpaid" | "unknown" | "nothing due"

export interface PaymentDue {
  issuer: string
  last4: string
  due: string
  daysLeft: number
  balance: number | null
  minimum: number | null
  paid: number
  state: PaymentState
  statement: string
  closed: string
}

export interface Payments {
  payments: PaymentDue[]
  seenUntil: string
  /** Card statements on disk whose due date has not been read yet. */
  unread: number
}

export interface DueDatesRead extends Payments {
  checked: number
  found: number
  failed: number
}

/* ------------------------------------------------------------ net worth */

export interface NetWorth {
  months: {
    month: string
    assets: number
    debts: number
    net: number
    /** Accounts carried from a statement more than 45 days old. */
    stale: string[]
    /** Some account had no statement yet by this month. */
    partial: boolean
  }[]
  accounts: {
    issuer: string
    last4: string
    kind: "bank" | "card" | "investment"
    balance: number
    asOf: string
    first: string
  }[]
  latest: NetWorth["months"][number] | null
}

/* ------------------------------------------------------------ sources */

export interface BankGuide {
  bank: string
  url: string
  help: string | null
  history: string | null
  steps: string[]
  exports: string[]
  bulk: string | null
  closes: string | null
}

export interface SourceAccount {
  issuer: string
  last4: string
  lastClosed: string
  /** When the next statement should be downloadable. */
  expected: string
  ready: boolean
  /** Statements likely out since the last one in the store. */
  missing: number
  guide: BankGuide | null
}

export interface WatchStatus {
  folder: string
  on: boolean
  since: number | null
  suggested: string
  log: { file: string; ok: boolean; at: number; transactions?: number | null; error?: string | null }[]
  error: string
}

export interface Sources {
  accounts: SourceAccount[]
  watch: WatchStatus
}

export interface Feed {
  connected: boolean
  synced: number | null
  errors: string[]
  accounts: { issuer: string; last4: string; balance: number | null; kind: string; asOf: string }[]
  /** How the feed agreed with each statement that later covered its days. */
  checks: {
    statement: string
    issuer: string
    last4: string
    periodEnd: string
    feedRows: number
    matched: number
    statementRows: number
    reconciled: 0 | 1 | null
  }[]
}
