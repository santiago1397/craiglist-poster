// Fleet-wide editing health.
//
// This lives on Diagnostics rather than on a post's own page because everything
// it says is a statement about the system, not about one ad: "editing is
// disabled", "outside edit window 08-19", "posting paused from the dashboard".
// Showing that on a healthy post's page would put someone else's problem in
// front of you every time you opened an unrelated listing.
//
// The counts are links. A number you cannot act on is decoration.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { PencilLine } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import type { EditHealth } from "../lib/edits";

export function EditHealthCard({ accounts }: { accounts: string[] }) {
  const q = useQuery({
    queryKey: ["edits", "health", accounts],
    queryFn: () =>
      api.get<EditHealth>("/edits/health", { accounts: accounts.join(",") }),
    enabled: accounts.length > 0,
    refetchInterval: 60_000,
  });

  const h = q.data;
  if (!h) return null;

  const tiles: { label: string; n: number; to?: string; tone?: "warn" | "danger" }[] = [
    { label: "Pending", n: h.pending, to: "/posts?edit=pending" },
    { label: "Applying", n: h.applying },
    { label: "Loading", n: h.hydrating },
    { label: "Parked", n: h.parked, to: "/posts?edit=parked", tone: "warn" },
    { label: "Degraded", n: h.degraded, to: "/posts?edit=degraded", tone: "danger" },
  ];

  return (
    <section className="rounded-lg border border-border bg-surface p-4 space-y-3">
      <h2 className="flex items-center gap-2 font-semibold">
        <PencilLine size={15} aria-hidden />
        Editing live posts
      </h2>

      <ul className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        {tiles.map((t) => {
          const hot = t.n > 0 && t.tone;
          const body = (
            <div
              className={cn(
                "rounded border px-2 py-1.5 text-center",
                hot === "danger"
                  ? "border-danger-border bg-danger text-danger-fg"
                  : hot === "warn"
                    ? "border-warn-border bg-warn text-warn-fg"
                    : "border-border bg-bg/40",
              )}
            >
              <div className="text-lg font-semibold tabular-nums">{t.n}</div>
              <div className="text-xs text-fg-muted">{t.label}</div>
            </div>
          );
          return (
            <li key={t.label}>
              {t.to && t.n > 0 ? (
                <Link to={t.to} className="block hover:opacity-90">
                  {body}
                </Link>
              ) : (
                body
              )}
            </li>
          );
        })}
      </ul>

      {h.global_blocks.length > 0 && (
        <div>
          <p className="text-xs text-fg-muted">
            Nothing will be applied to a live posting while any of these hold:
          </p>
          <ul className="mt-1 list-disc pl-5 text-xs text-fg-muted space-y-0.5">
            {h.global_blocks.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
