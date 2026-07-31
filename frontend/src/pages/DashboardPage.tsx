import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatDateTime, formatNumber, formatRelative } from "../lib/format";
import { cn } from "../lib/cn";

type DashboardAccount = {
  account: string;
  eligible_now: boolean | null;
  next_eligible_at: string | null;
  block_reasons: string[] | null;
  posts_last_24h_total: number | null;
  posts_last_7d_this_account: number | null;
  stats_sync_health: { ok: boolean; last_run_ts?: string; error_type?: string } | null;
  state_ts: string | null;
  photos_total: number | null;
  photos_never_used: number | null;
  photos_eligible: number | null;
  covers_total: number | null;
  covers_never_used: number | null;
  covers_eligible: number | null;
  inventory_ts: string | null;
  last_success_ts: string | null;
  last_success_url: string | null;
  last_success_title: string | null;
  last_success_post_id: string | null;
  last_attempt_ts: string | null;
  last_attempt_outcome: string | null;
  last_attempt_error_type: string | null;
  last_attempt_error_message: string | null;
};

type DashboardResp = { accounts: DashboardAccount[] };

export default function DashboardPage() {
  const q = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<DashboardResp>("/dashboard"),
    refetchInterval: 30_000,
  });

  if (q.isLoading) return <div className="p-6 text-fg-muted">Loading…</div>;
  if (q.isError) return <div className="p-6 text-danger-fg">Failed to load dashboard.</div>;

  const accounts = q.data?.accounts ?? [];

  return (
    <div className="p-4 sm:p-6 space-y-4">
      <h1 className="text-lg font-semibold">Dashboard</h1>
      <div className="grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
        {accounts.map((a) => (
          <AccountCard key={a.account} a={a} />
        ))}
        {accounts.length === 0 && (
          <div className="text-fg-muted">No account data yet. Waiting for the reporter to send heartbeats.</div>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium",
        ok ? "bg-ok text-ok-fg border border-ok-border" : "bg-danger text-danger-fg border border-danger-border",
      )}
    >
      {label}
    </span>
  );
}

function OutcomeBadge({ outcome }: { outcome: string | null }) {
  if (!outcome) return <span className="text-fg-subtle text-xs">—</span>;
  const ok = outcome === "posted" || outcome === "dry_run";
  return <StatusBadge ok={ok} label={outcome.replace(/_/g, " ")} />;
}

function AccountCard({ a }: { a: DashboardAccount }) {
  const eligible = a.eligible_now === true;
  return (
    <div className="rounded-lg border border-border bg-surface p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">{a.account}</h2>
        <StatusBadge ok={eligible} label={eligible ? "eligible" : "blocked"} />
      </div>

      {!eligible && a.block_reasons && a.block_reasons.length > 0 && (
        <ul className="text-xs text-fg-muted space-y-0.5">
          {a.block_reasons.map((r, i) => (
            <li key={i}>• {r}</li>
          ))}
        </ul>
      )}

      <div className="text-sm">
        <div className="text-fg-muted text-xs uppercase tracking-wide">Next eligible</div>
        <div>
          {a.next_eligible_at ? (
            <>
              {formatDateTime(a.next_eligible_at)}{" "}
              <span className="text-fg-subtle text-xs">({formatRelative(a.next_eligible_at)})</span>
            </>
          ) : (
            <span className="text-fg-subtle">—</span>
          )}
        </div>
      </div>

      <div className="text-sm">
        <div className="text-fg-muted text-xs uppercase tracking-wide">Last post</div>
        {a.last_success_ts ? (
          <div className="space-y-0.5">
            <div className="text-xs text-fg-subtle">
              {formatDateTime(a.last_success_ts)} ({formatRelative(a.last_success_ts)})
            </div>
            <div className="truncate">{a.last_success_title || "(no title)"}</div>
            {a.last_success_url && (
              <a
                href={a.last_success_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-info-fg hover:underline break-all"
              >
                {a.last_success_url}
              </a>
            )}
          </div>
        ) : (
          <div className="text-fg-subtle">—</div>
        )}
      </div>

      <div className="text-sm">
        <div className="text-fg-muted text-xs uppercase tracking-wide">Last attempt</div>
        {a.last_attempt_ts ? (
          <div className="flex items-center gap-2">
            <OutcomeBadge outcome={a.last_attempt_outcome} />
            <span className="text-xs text-fg-subtle">{formatRelative(a.last_attempt_ts)}</span>
            {a.last_attempt_error_type && (
              <span className="text-xs text-danger-fg">— {a.last_attempt_error_type}</span>
            )}
          </div>
        ) : (
          <div className="text-fg-subtle">—</div>
        )}
      </div>

      <div className="text-sm">
        <div className="text-fg-muted text-xs uppercase tracking-wide">Photos</div>
        <div>
          {a.photos_never_used !== null ? (
            <>
              <span className="font-medium">{formatNumber(a.photos_never_used)}</span> never-used /{" "}
              {formatNumber(a.photos_total)} total
              <span className="text-fg-subtle text-xs">
                {" — "}{formatNumber(a.photos_eligible)} eligible now
              </span>
            </>
          ) : (
            <span className="text-fg-subtle">— (waiting for photo-inventory)</span>
          )}
        </div>
        <div className="text-xs text-fg-subtle mt-0.5">
          covers: {formatNumber(a.covers_total)}
        </div>
      </div>

      <div className="pt-2 border-t border-border text-xs text-fg-subtle flex justify-between">
        <span>
          {formatNumber(a.posts_last_24h_total)} in 24h / {formatNumber(a.posts_last_7d_this_account)} this week
        </span>
        {a.stats_sync_health && (
          <span className={a.stats_sync_health.ok ? "text-ok-fg" : "text-danger-fg"}>
            stats-sync {a.stats_sync_health.ok ? "OK" : a.stats_sync_health.error_type || "failing"}
          </span>
        )}
      </div>
    </div>
  );
}
