/**
 * Formatting, in one place.
 *
 * Money and dates appear on every screen, and the fastest way to make a
 * dashboard feel unreliable is to print the same number two different ways on
 * two different pages.
 */

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
})

const usdCents = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** `$1,234`. Pass cents when the exact figure matters (a single charge). */
export function money(v: number | null | undefined, cents = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—"
  return cents ? usdCents.format(v) : usd.format(v)
}

/** `$1,234` / `-$1,234`, but always with an explicit sign. For deltas. */
export function signedMoney(v: number | null | undefined, cents = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—"
  const s = money(Math.abs(v), cents)
  return v < 0 ? `−${s}` : `+${s}`
}

/** Compact, for axis ticks where space is tight: `$1.2k`. */
export function moneyCompact(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—"
  const a = Math.abs(v)
  const sign = v < 0 ? "−" : ""
  if (a >= 1_000_000) return `${sign}$${(a / 1_000_000).toFixed(1)}M`
  if (a >= 1_000) return `${sign}$${(a / 1_000).toFixed(a >= 10_000 ? 0 : 1)}k`
  return `${sign}$${Math.round(a)}`
}

export function percent(v: number | null | undefined, dp = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—"
  return `${v.toFixed(dp)}%`
}

export function signedPercent(v: number | null | undefined, dp = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—"
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(dp)}%`
}

/** `2026-03` -> `Mar 2026`. */
export function monthLabel(m: string): string {
  const [y, mo] = m.split("-")
  const d = new Date(Number(y), Number(mo) - 1, 1)
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" })
}

/** `2026-03` -> `Mar`. For dense axes where the year is in the title. */
export function monthShort(m: string): string {
  const [y, mo] = m.split("-")
  return new Date(Number(y), Number(mo) - 1, 1).toLocaleDateString("en-US", {
    month: "short",
  })
}

/** `2026-03-14` -> `14 Mar 2026`. */
export function dateLabel(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString("en-US", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  })
}

/** `14 Mar`, for tight rows where the year is implied. */
export function dateShort(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString("en-US", { day: "2-digit", month: "short" })
}

export function daysAgo(iso: string | null | undefined): number | null {
  if (!iso) return null
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(d.getTime())) return null
  return Math.round((Date.now() - d.getTime()) / 86_400_000)
}

export function relativeDay(iso: string | null | undefined): string {
  const n = daysAgo(iso)
  if (n === null) return "—"
  if (n === 0) return "today"
  if (n === 1) return "yesterday"
  if (n === -1) return "tomorrow"
  if (n < 0) return `in ${-n} days`
  if (n < 30) return `${n} days ago`
  if (n < 365) return `${Math.round(n / 30)} months ago`
  return `${(n / 365).toFixed(1)} years ago`
}

export const cadenceLabel: Record<string, string> = {
  weekly: "Weekly",
  biweekly: "Every 2 weeks",
  monthly: "Monthly",
  quarterly: "Quarterly",
  semiannual: "Twice a year",
  annual: "Yearly",
}

/** Strip the extension so a filename reads as a title. */
export function fileLabel(name: string): string {
  return name.replace(/\.pdf$/i, "").replace(/[_]+/g, " ")
}
