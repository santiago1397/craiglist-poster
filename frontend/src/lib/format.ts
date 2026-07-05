// All timestamps render in ET per the product decision.
const ET = "America/New_York";

const dtf = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const dtfDate = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  year: "numeric",
  month: "short",
  day: "2-digit",
});

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return dtf.format(d) + " ET";
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return dtfDate.format(d);
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = Date.now();
  const diffSec = Math.round((d.getTime() - now) / 1000);
  const abs = Math.abs(diffSec);
  const past = diffSec < 0;
  if (abs < 60) return past ? "just now" : "any moment";
  if (abs < 3600) return `${Math.round(abs / 60)}m ${past ? "ago" : ""}`.trim();
  if (abs < 86400) return `${Math.round(abs / 3600)}h ${past ? "ago" : ""}`.trim();
  if (abs < 30 * 86400) return `${Math.round(abs / 86400)}d ${past ? "ago" : ""}`.trim();
  return formatDate(iso);
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("en-US").format(n);
}

export function formatRate(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(1);
}
