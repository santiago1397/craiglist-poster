// Health at a glance, in the header, on every page.
//
// The two things worth knowing on a phone — is it still posting, and is
// anything stuck — lived only inside /review, below three stacked sections and
// under the fold. This surfaces them everywhere and links straight to the fix.
//
// Deliberately reuses the endpoints Review already calls rather than adding a
// summary route; React Query dedupes them, so opening Review costs nothing
// extra.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  PauseCircle,
} from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";

type PostingState = { enabled: boolean; paused_reason: string | null };
type Health = {
  needs_attention: number;
  unreviewed: number;
  stuck_claims: number;
  accounts: Record<string, { queue_depth: number }>;
};
type DiagSummary = { counts: { critical: number; warning: number }; total: number };

const POLL = 60_000;

export function StatusPill({ className }: { className?: string }) {
  const posting = useQuery({
    queryKey: ["settings", "posting"],
    queryFn: () => api.get<PostingState>("/settings/posting"),
    refetchInterval: POLL,
  });

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<{ accounts: string[] }>("/accounts"),
    staleTime: 5 * 60_000,
  });

  const list = accounts.data?.accounts ?? [];
  const health = useQuery({
    queryKey: ["drafts", "health", list],
    queryFn: () => api.get<Health>("/drafts/health", { accounts: list.join(",") }),
    enabled: list.length > 0,
    refetchInterval: POLL,
  });

  const diag = useQuery({
    queryKey: ["diagnostics", "summary"],
    queryFn: () => api.get<DiagSummary>("/diagnostics/summary"),
    refetchInterval: POLL,
  });

  // Nothing to say yet — render nothing rather than a flickering placeholder.
  if (posting.isLoading && !posting.data) return null;

  const paused = posting.data && !posting.data.enabled;
  const attention = health.data?.needs_attention ?? 0;
  const critical = diag.data?.counts.critical ?? 0;
  const dryQueues = Object.entries(health.data?.accounts ?? {}).filter(
    ([, a]) => a.queue_depth === 0,
  ).length;

  // Ordered by how much it costs you. A critical problem outranks everything
  // because it is the only state that can mean "a live ad is wrong right now"
  // or "the machine stopped reporting and no other signal will ever fire".
  const state = critical > 0
    ? {
        tone: "danger" as const,
        Icon: AlertOctagon,
        label: `${critical} critical`,
        full: `${critical} critical problem${critical === 1 ? "" : "s"}`,
        to: "/diagnostics",
      }
    : paused
    ? {
        tone: "warn" as const,
        Icon: PauseCircle,
        label: "Paused",
        full: "Posting paused",
        to: "/review",
      }
      : attention > 0
      ? {
          tone: "warn" as const,
          Icon: AlertTriangle,
          label: `${attention} stuck`,
          full: `${attention} draft${attention === 1 ? "" : "s"} need attention`,
          to: "/review",
        }
      : dryQueues > 0
        ? {
            tone: "danger" as const,
            Icon: CircleSlash,
            label: "Queue empty",
            full: `${dryQueues} account${dryQueues === 1 ? "" : "s"} have no queued drafts`,
            to: "/review",
          }
        : {
            tone: "ok" as const,
            Icon: CheckCircle2,
            label: "Active",
            full: "Posting is active",
            to: "/review",
          };

  const { Icon } = state;

  return (
    <Link
      to={state.to}
      title={state.full}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        state.tone === "ok" && "bg-ok text-ok-fg border-ok-border",
        state.tone === "warn" && "bg-warn text-warn-fg border-warn-border",
        state.tone === "danger" && "bg-danger text-danger-fg border-danger-border",
        className,
      )}
    >
      <Icon size={14} aria-hidden="true" />
      {/* Icon + colour alone would exclude colourblind users and screen
          readers, so the words are always present — just abbreviated when
          space is tight. */}
      <span className="sm:hidden">{state.label}</span>
      <span className="hidden sm:inline">{state.full}</span>
    </Link>
  );
}
