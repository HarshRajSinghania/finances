import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** The fixed categorical order. Assigned by index, never cycled. */
export const SERIES = [
  "var(--color-series-1)",
  "var(--color-series-2)",
  "var(--color-series-3)",
  "var(--color-series-4)",
  "var(--color-series-5)",
] as const

/**
 * Past five series, fold the tail into "Other" rather than inventing a sixth
 * hue — a generated colour is the point where a palette stops being readable.
 */
export function topNWithOther<T>(
  rows: T[],
  n: number,
  value: (r: T) => number,
  label: (r: T) => string,
): { label: string; value: number; isOther: boolean }[] {
  const sorted = [...rows].sort((a, b) => value(b) - value(a))
  const head = sorted.slice(0, n).map((r) => ({
    label: label(r),
    value: value(r),
    isOther: false,
  }))
  const tail = sorted.slice(n)
  if (tail.length) {
    head.push({
      label: `Other (${tail.length})`,
      value: tail.reduce((s, r) => s + value(r), 0),
      isOther: true,
    })
  }
  return head
}
