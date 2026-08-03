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
  // Live depth of the server-side stack, counted per account. An unclaimed
  // image is available to every account, so these do not sum to a stack total.
  photos_available: number;
  covers_available: number;
  images_assigned: number;
  images_published: number;
  queued_drafts: number;
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
      <div className="grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-4">
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

// Craigslist takes 24 images: one cover plus 23 photos.
const PHOTOS_PER_POST = 23;

/** What this account can actually put on a post, and whether it is enough.
 *
 * This block used to read `photo_inventory`, which counts files in
 * `data/photos/` on the Windows box — folders nothing has read since images
 * moved server-side. It reported 64 usable photos against a real stack of 5.
 * Everything here is counted live against the images table instead.
 */
function ImageStack({ a }: { a: DashboardAccount }) {
  const demand = a.queued_drafts * PHOTOS_PER_POST;
  const short = Math.max(0, demand - a.photos_available);
  return (
    <div className="text-sm">
      <div className="text-fg-muted text-xs uppercase tracking-wide">Image stack</div>
      <div className="flex gap-4 flex-wrap">
        <span>
          <span
            className={cn("font-medium", a.covers_available === 0 && "text-danger-fg")}
          >
            {formatNumber(a.covers_available)}
          </span>{" "}
          <span className="text-fg-subtle text-xs">covers</span>
        </span>
        <span>
          <span className={cn("font-medium", short > 0 && "text-warn-fg")}>
            {formatNumber(a.photos_available)}
          </span>{" "}
          <span className="text-fg-subtle text-xs">photos free</span>
        </span>
        <span className="text-fg-subtle text-xs">
          {formatNumber(a.images_assigned)} assigned · {formatNumber(a.images_published)} published
        </span>
      </div>
      {a.covers_available === 0 && (
        <div className="text-xs text-danger-fg mt-0.5">
          No cover free — a roof photo becomes the thumbnail.
        </div>
      )}
      {short > 0 && (
        <div className="text-xs text-warn-fg mt-0.5">
          Short {formatNumber(short)} for {a.queued_drafts} queued draft
          {a.queued_drafts === 1 ? "" : "s"} — they publish thinner.
        </div>
      )}
    </div>
  );
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

      <ImageStack a={a} />

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
