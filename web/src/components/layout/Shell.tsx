import {
  AlertTriangle,
  CalendarHeart,
  FileText,
  Gauge,
  LayoutDashboard,
  MessageSquareText,
  Monitor,
  Moon,
  Receipt,
  RefreshCw,
  Repeat,
  Search,
  Sheet,
  Sun,
} from "lucide-react"
import { NavLink, Outlet, useLocation } from "react-router-dom"

import { ClientSwitcher } from "@/components/ClientSwitcher"
import { PhoneShare } from "@/components/PhoneShare"
import { TrustPill } from "@/components/TrustPill"
import { Button } from "@/components/ui/button"
import { Skeleton, TooltipProvider } from "@/components/ui/misc"
import { useTheme, type Theme } from "@/hooks/useApi"
import { useReadOnly } from "@/hooks/useSession"
import { dateLabel } from "@/lib/format"
import type { Summary } from "@/lib/types"
import { cn } from "@/lib/utils"

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/statements", label: "Statements", icon: FileText },
  { to: "/transactions", label: "Transactions", icon: Receipt },
  { to: "/subscriptions", label: "Subscriptions", icon: Repeat },
  { to: "/insights", label: "Insights", icon: Search },
  { to: "/ask", label: "Ask", icon: MessageSquareText },
  { to: "/plan", label: "Budgets & goals", icon: Gauge },
  { to: "/wrapped", label: "Wrapped", icon: CalendarHeart },
]

export function Shell({
  summary,
  loading,
  onRefresh,
}: {
  summary: Summary | null
  loading: boolean
  onRefresh: () => void
}) {
  const { theme, setTheme } = useTheme()
  const { pathname } = useLocation()
  const readOnly = useReadOnly()
  // The phone view cannot ask: questions need a key or a model on the computer.
  const nav = readOnly ? NAV.filter((n) => n.to !== "/ask") : NAV
  const current = NAV.find((n) => (n.end ? pathname === n.to : pathname.startsWith(n.to)))

  return (
    <TooltipProvider delayDuration={200}>
      <div className="min-h-screen lg:grid lg:grid-cols-[15rem_1fr]">
        {/* ---------------------------------------------------------- rail */}
        <aside className="sticky top-0 z-30 hidden h-screen flex-col border-r border-[var(--color-hairline)] bg-[var(--color-surface)] lg:flex">
          <div className="px-5 py-5">
            <p className="text-sm font-semibold tracking-tight">finances</p>
            <p className="mt-0.5 text-xs text-[var(--color-ink-muted)]">
              from your statements
            </p>
          </div>
          <ClientSwitcher />

          <nav className="flex-1 space-y-0.5 px-3">
            {nav.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                    isActive
                      ? "bg-[var(--color-plane)] font-medium text-[var(--color-ink)]"
                      : "text-[var(--color-ink-secondary)] hover:bg-[var(--color-plane)]",
                  )
                }
              >
                <Icon className="size-4 shrink-0" strokeWidth={1.75} />
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="space-y-3 border-t border-[var(--color-hairline)] p-4">
            {summary && (
              <dl className="space-y-1 text-xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-[var(--color-ink-muted)]">Period</dt>
                  <dd className="tnum text-right text-[var(--color-ink-secondary)]">
                    {summary.monthsCovered} months
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-[var(--color-ink-muted)]">Accounts</dt>
                  <dd className="tnum text-[var(--color-ink-secondary)]">
                    {summary.accounts.length}
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-[var(--color-ink-muted)]">Rows</dt>
                  <dd className="tnum text-[var(--color-ink-secondary)]">
                    {summary.transactions.toLocaleString()}
                  </dd>
                </div>
              </dl>
            )}
            <ThemeToggle theme={theme} setTheme={setTheme} />
          </div>
        </aside>

        {/* --------------------------------------------------------- main */}
        <div className="flex min-w-0 flex-col">
          <header className="sticky top-0 z-20 border-b border-[var(--color-hairline)] bg-[var(--color-plane)]/85 backdrop-blur">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3 sm:px-8">
              <div className="min-w-0 flex-1">
                <h1 className="truncate text-base font-semibold tracking-tight">
                  {current?.label ?? "Overview"}
                </h1>
                {summary?.firstDate && (
                  <p className="truncate text-xs text-[var(--color-ink-muted)]">
                    {dateLabel(summary.firstDate)} – {dateLabel(summary.lastDate)}
                  </p>
                )}
              </div>

              <div className="flex items-center gap-2">
                {loading && !summary ? (
                  <Skeleton className="h-6 w-28 rounded-full" />
                ) : (
                  summary && <TrustPill coverage={summary.coverage} />
                )}
                {summary && summary.transactions > 0 && (
                  <Button variant="ghost" size="sm" asChild>
                    <a
                      href="/api/export.xlsx"
                      download
                      aria-label="Download everything as an Excel workbook"
                      title="Transactions, months, categories, subscriptions and statements, one sheet each"
                    >
                      <Sheet />
                      <span className="hidden sm:inline">Excel</span>
                    </a>
                  </Button>
                )}
                {readOnly && (
                  <span className="rounded-full border border-[var(--color-hairline)] px-2 py-0.5 text-xs text-[var(--color-ink-secondary)]">
                    Read-only
                  </span>
                )}
                <PhoneShare />
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={onRefresh}
                  aria-label="Reload data"
                  title="Reload data"
                >
                  <RefreshCw className={cn(loading && "animate-spin")} />
                </Button>
              </div>
            </div>

            {/* The rail is desktop-only; below lg the nav moves in here. */}
            <nav className="scroll-x flex gap-1 border-t border-[var(--color-hairline)] px-3 py-1.5 lg:hidden">
              {nav.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs whitespace-nowrap",
                      isActive
                        ? "bg-[var(--color-surface)] font-medium"
                        : "text-[var(--color-ink-secondary)]",
                    )
                  }
                >
                  <Icon className="size-3.5" strokeWidth={1.75} />
                  {label}
                </NavLink>
              ))}
            </nav>
          </header>

          <main className="min-w-0 flex-1 px-5 py-6 sm:px-8">
            {summary && <FxNotice missing={summary.fxMissing} base={summary.currency} />}
            <Outlet />
          </main>

          <footer className="px-5 pb-6 sm:px-8">
            <p className="text-xs text-[var(--color-ink-muted)]">
              Read from your own statements. Nothing leaves this machine unless you ask Claude or OpenAI.
            </p>
          </footer>
        </div>
      </div>
    </TooltipProvider>
  )
}

function ThemeToggle({
  theme,
  setTheme,
}: {
  theme: Theme
  setTheme: (t: Theme) => void
}) {
  const options: { value: Theme; icon: React.ElementType; label: string }[] = [
    { value: "light", icon: Sun, label: "Light" },
    { value: "dark", icon: Moon, label: "Dark" },
    { value: "system", icon: Monitor, label: "System" },
  ]
  return (
    <div
      className="flex rounded-md border border-[var(--color-hairline)] p-0.5"
      role="group"
      aria-label="Colour theme"
    >
      {options.map(({ value, icon: Icon, label }) => (
        <button
          key={value}
          type="button"
          onClick={() => setTheme(value)}
          aria-pressed={theme === value}
          title={label}
          className={cn(
            "flex flex-1 items-center justify-center rounded-[5px] py-1 transition-colors",
            theme === value
              ? "bg-[var(--color-plane)] text-[var(--color-ink)]"
              : "text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]",
          )}
        >
          <Icon className="size-3.5" strokeWidth={1.75} />
          <span className="sr-only">{label}</span>
        </button>
      ))}
    </div>
  )
}

/**
 * Rows in a currency with no exchange rate are left out of every total rather
 * than added to the base currency as if they were the same money. Saying so on
 * every page keeps a smaller total from looking like a smaller life.
 */
function FxNotice({
  missing,
  base,
}: {
  missing: Record<string, number> | undefined
  base: string | undefined
}) {
  const entries = Object.entries(missing ?? {})
  if (entries.length === 0) return null
  return (
    <div
      role="status"
      className="mb-5 flex items-start gap-2.5 rounded-[var(--radius-card)] border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 px-4 py-3 text-xs leading-relaxed"
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
      <div className="space-y-1">
        {entries.map(([cur, n]) => (
          <p key={cur}>
            <span className="tnum font-medium">
              {n.toLocaleString()} {cur}
            </span>{" "}
            {n === 1 ? "transaction isn't" : "transactions aren't"} counted in these{" "}
            {base ?? ""} totals — set a rate with{" "}
            <code className="rounded bg-[var(--color-plane)] px-1 py-0.5 font-mono">
              finances fx {cur}=&lt;rate&gt;
            </code>
          </p>
        ))}
      </div>
    </div>
  )
}
