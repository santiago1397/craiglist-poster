// One page that answers "what is broken?" without a shell on the VPS.
//
// Everything the desktop reports has always been durable — the outbox survives
// outages, every event is idempotent — but half of it landed in `flow_errors`,
// a table with no endpoint and no screen. This is the read side.
//
// Each row is one problem: what happened, what it means in plain English, and
// where to go to fix it. The explanation is not decoration. "TimeoutError" and
// "the selector Craigslist serves has changed, open the HTML dump to find the
// new one" are the same fact, and only one of them is useful on a Monday.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EditHealthCard } from "../components/EditHealthCard";
import {
  AlertOctagon,
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileCode2,
  ImageIcon,
  Info,
  RefreshCw,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import { formatDateTime, formatRelative } from "../lib/format";

type Problem = {
  id: string;
  kind: "flow_error" | "post_failure" | "machine_silent" | "stuck_claim" | "image_stack";
  severity: "critical" | "warning" | "info";
  ts: string;
  machine: string | null;
  account: string | null;
  flow: string | null;
  step: string | null;
  title: string;
  detail: string | null;
  explanation: string;
  where: string | null;
  context: Record<string, unknown>;
  artifact_ids: string[];
  acknowledged_at: string | null;
};

type Feed = {
  window_hours: number;
  counts: { critical: number; warning: number; info: number };
  total: number;
  problems: Problem[];
};

const POLL = 30_000;

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 72, label: "3d" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
] as const;

// Only flow errors can be acknowledged. The rest are derived from live state
// and clear themselves when the condition clears.
const ACKNOWLEDGEABLE = new Set(["flow_error"]);

const SEVERITY = {
  critical: {
    Icon: AlertOctagon,
    chip: "bg-danger text-danger-fg border-danger-border",
    label: "Critical",
    hint: "Posting is stopped, or a live ad is wrong right now",
  },
  warning: {
    Icon: AlertTriangle,
    chip: "bg-warn text-warn-fg border-warn-border",
    label: "Warning",
    hint: "Something failed but the system routed around it",
  },
  info: {
    Icon: Info,
    chip: "bg-surface-2 text-fg-muted border-border",
    label: "Info",
    hint: "Worth knowing, nothing is broken",
  },
} as const;

const KIND_LABEL: Record<Problem["kind"], string> = {
  flow_error: "Background job",
  post_failure: "Posting run",
  machine_silent: "Machine offline",
  stuck_claim: "Stuck draft",
  image_stack: "Image stack",
};

function ArtifactLinks({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 pt-1">
      {ids.map((id, i) => (
        <a
          key={id}
          href={`${import.meta.env.VITE_API_BASE_URL || "/api"}/artifacts/${id}`}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-xs text-fg-muted hover:text-fg hover:bg-surface-2"
        >
          {/* Screenshot and HTML dump are spooled as a pair, screenshot first. */}
          {i % 2 === 0 ? <ImageIcon size={13} /> : <FileCode2 size={13} />}
          {i % 2 === 0 ? "Screenshot" : "Page HTML"}
        </a>
      ))}
    </div>
  );
}

function ProblemRow({
  problem,
  expanded,
  onToggle,
  onAcknowledge,
  acknowledging,
}: {
  problem: Problem;
  expanded: boolean;
  onToggle: () => void;
  onAcknowledge: () => void;
  acknowledging: boolean;
}) {
  const sev = SEVERITY[problem.severity];
  const { Icon } = sev;
  const canAck = ACKNOWLEDGEABLE.has(problem.kind) && !problem.acknowledged_at;

  return (
    <li className="border border-border rounded-lg overflow-hidden bg-surface">
      <button
        onClick={onToggle}
        aria-expanded={expanded}
        className="w-full flex items-start gap-3 p-3 text-left hover:bg-surface-2/50"
      >
        <span className={cn("mt-0.5 shrink-0 rounded-full border p-1", sev.chip)}>
          <Icon size={14} aria-hidden="true" />
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="font-medium text-sm">{problem.title}</span>
            <span className="text-xs text-fg-subtle">{KIND_LABEL[problem.kind]}</span>
            {problem.account && (
              <span className="text-xs text-fg-subtle">· {problem.account}</span>
            )}
            {problem.machine && (
              <span className="text-xs text-fg-subtle">· {problem.machine}</span>
            )}
          </span>
          {problem.detail && (
            <span className="mt-1 block truncate text-xs text-fg-muted">
              {problem.detail}
            </span>
          )}
        </span>

        <span className="flex shrink-0 items-center gap-2">
          <span className="text-xs text-fg-subtle" title={formatDateTime(problem.ts)}>
            {formatRelative(problem.ts)}
          </span>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-border p-3 space-y-3 text-sm">
          {/* The explanation comes first: it is the reason to open the row. */}
          <p className="text-fg-muted leading-relaxed">{problem.explanation}</p>

          {problem.detail && (
            <pre className="overflow-x-auto rounded bg-surface-2 p-2 text-xs text-fg-muted whitespace-pre-wrap break-words">
              {problem.detail}
            </pre>
          )}

          <ArtifactLinks ids={problem.artifact_ids} />

          {Object.keys(problem.context).length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer text-fg-subtle hover:text-fg">
                Context
              </summary>
              <pre className="mt-2 overflow-x-auto rounded bg-surface-2 p-2 text-fg-muted">
                {JSON.stringify(problem.context, null, 2)}
              </pre>
            </details>
          )}

          <div className="flex flex-wrap items-center gap-2 pt-1">
            {problem.where && (
              <Link
                to={problem.where}
                className="inline-flex items-center gap-1.5 rounded border border-border px-2.5 py-1 text-xs hover:bg-surface-2"
              >
                <ExternalLink size={13} />
                Go to {problem.where.replace("/", "") || "dashboard"}
              </Link>
            )}
            {canAck && (
              <button
                onClick={onAcknowledge}
                disabled={acknowledging}
                className="inline-flex items-center gap-1.5 rounded border border-border px-2.5 py-1 text-xs hover:bg-surface-2 disabled:opacity-50"
              >
                <Check size={13} />
                Mark as seen
              </button>
            )}
            <span className="text-xs text-fg-subtle">{formatDateTime(problem.ts)}</span>
          </div>
        </div>
      )}
    </li>
  );
}

export default function DiagnosticsPage() {
  const qc = useQueryClient();
  const [hours, setHours] = useState<number>(72);
  const [severity, setSeverity] = useState<string>("");
  const [showAcknowledged, setShowAcknowledged] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<{ accounts: string[] }>("/accounts"),
    staleTime: 5 * 60_000,
  });

  const feed = useQuery({
    queryKey: ["diagnostics", hours, showAcknowledged],
    queryFn: () =>
      api.get<Feed>("/diagnostics", {
        hours,
        include_acknowledged: String(showAcknowledged),
      }),
    refetchInterval: POLL,
  });

  const ack = useMutation({
    mutationFn: (ids: string[]) =>
      api.post<{ acknowledged: number }>("/diagnostics/acknowledge", { event_ids: ids }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["diagnostics"] });
    },
  });

  const problems = (feed.data?.problems ?? []).filter(
    (p) => !severity || p.severity === severity,
  );
  const counts = feed.data?.counts;

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const openFlowErrors = problems
    .filter((p) => ACKNOWLEDGEABLE.has(p.kind) && !p.acknowledged_at)
    .map((p) => p.id);

  return (
    <div className="mx-auto max-w-5xl p-3 sm:p-6 space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">Diagnostics</h1>
        <p className="text-sm text-fg-muted">
          Every failure the desktop and this server have recorded, newest and most
          severe first. Screenshots and page dumps are attached where they exist.
        </p>
      </header>

      <EditHealthCard accounts={accountsQ.data?.accounts ?? []} />

      {/* Severity counts double as filters — the number and the way to see what
          it refers to should not be two separate controls. */}
      <div className="flex flex-wrap gap-2">
        {(["critical", "warning", "info"] as const).map((key) => {
          const s = SEVERITY[key];
          const n = counts?.[key] ?? 0;
          const active = severity === key;
          return (
            <button
              key={key}
              onClick={() => setSeverity(active ? "" : key)}
              aria-pressed={active}
              title={s.hint}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm",
                active ? s.chip : "border-border hover:bg-surface-2",
              )}
            >
              <s.Icon size={15} aria-hidden="true" />
              <span className="font-medium">{n}</span>
              <span>{s.label}</span>
            </button>
          );
        })}

        <div className="ml-auto flex items-center gap-2">
          <div role="radiogroup" aria-label="Time window" className="inline-flex rounded-md border border-border p-0.5">
            {WINDOWS.map((w) => (
              <button
                key={w.hours}
                role="radio"
                aria-checked={hours === w.hours}
                onClick={() => setHours(w.hours)}
                className={cn(
                  "rounded px-2 py-1 text-xs",
                  hours === w.hours ? "bg-surface-2 text-fg" : "text-fg-muted hover:text-fg",
                )}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button
            onClick={() => feed.refetch()}
            aria-label="Refresh"
            className="rounded border border-border p-2 text-fg-muted hover:text-fg hover:bg-surface-2"
          >
            <RefreshCw size={14} className={cn(feed.isFetching && "animate-spin")} />
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-xs text-fg-subtle">
        <label className="inline-flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={showAcknowledged}
            onChange={(e) => setShowAcknowledged(e.target.checked)}
          />
          Show acknowledged
        </label>
        {openFlowErrors.length > 1 && (
          <button
            onClick={() => ack.mutate(openFlowErrors)}
            disabled={ack.isPending}
            className="rounded border border-border px-2 py-1 hover:bg-surface-2 disabled:opacity-50"
          >
            Mark all {openFlowErrors.length} background errors as seen
          </button>
        )}
      </div>

      {feed.isLoading ? (
        <p className="text-sm text-fg-muted">Loading…</p>
      ) : problems.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface p-8 text-center">
          <Check className="mx-auto mb-2 text-ok-fg" size={28} />
          <p className="font-medium">Nothing is failing</p>
          <p className="mt-1 text-sm text-fg-muted">
            No errors in the last {feed.data?.window_hours ?? hours} hours.
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {problems.map((p) => (
            <ProblemRow
              key={p.id}
              problem={p}
              expanded={expanded.has(p.id)}
              onToggle={() => toggle(p.id)}
              onAcknowledge={() => ack.mutate([p.id])}
              acknowledging={ack.isPending}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
