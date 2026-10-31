import { Route, Routes } from "react-router-dom"

import { Shell } from "@/components/layout/Shell"
import { ErrorState } from "@/components/ui/state"
import { useApi } from "@/hooks/useApi"
import { api } from "@/lib/api"
import { Insights } from "@/pages/Insights"
import { Overview } from "@/pages/Overview"
import { Statements } from "@/pages/Statements"
import { Subscriptions } from "@/pages/Subscriptions"
import { Transactions } from "@/pages/Transactions"

export default function App() {
  // The summary carries the reconciliation verdict shown in the header on every
  // screen, so it is fetched once here and handed down rather than re-requested
  // per page. `reload` is passed to pages that mutate the store (uploads,
  // deletes) so the header cannot drift out of date after a change.
  const { data: summary, error, loading, refetch } = useApi(() => api.summary(), [])

  if (error && !summary) {
    return (
      <div className="grid min-h-screen place-items-center p-6">
        <div className="w-full max-w-lg rounded-[var(--radius-card)] border border-[var(--color-hairline)] bg-[var(--color-surface)]">
          <ErrorState error={error} onRetry={refetch} />
          <div className="border-t border-[var(--color-hairline)] px-6 py-4">
            <p className="text-xs leading-relaxed text-[var(--color-ink-secondary)]">
              This dashboard reads a local API. Start it from the project root:
            </p>
            <pre className="mt-2 overflow-x-auto rounded-md bg-[var(--color-plane)] px-3 py-2 font-mono text-xs">
              python api.py
            </pre>
          </div>
        </div>
      </div>
    )
  }

  return (
    <Routes>
      <Route element={<Shell summary={summary} loading={loading} onRefresh={refetch} />}>
        <Route index element={<Overview summary={summary} loading={loading} />} />
        <Route
          path="statements"
          element={<Statements summary={summary} onChanged={refetch} />}
        />
        <Route path="transactions" element={<Transactions />} />
        <Route path="subscriptions" element={<Subscriptions />} />
        <Route path="insights" element={<Insights />} />
        <Route
          path="*"
          element={
            <p className="text-sm text-[var(--color-ink-secondary)]">
              Nothing here. Pick a section from the menu.
            </p>
          }
        />
      </Route>
    </Routes>
  )
}
