import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { formatDate, formatDateTime, formatNumber } from "../lib/format";

type Post = {
  post_id: string;
  account: string;
  title: string | null;
  url: string | null;
  posted_ts: string | null;
  source: string | null;
};

type Snapshot = {
  snapshot_date: string;
  snapshot_ts_utc: string;
  status: string | null;
  impressions: number | null;
  views: number | null;
  shares: number | null;
  favorites: number | null;
  area: string | null;
  category: string | null;
  expires_in_days: number | null;
  autorepost: string | null;
  freshness_note: string | null;
};

type GhostEntry = { ts: string; ghosted: boolean };

type Detail = { post: Post; snapshots: Snapshot[]; ghost_history: GhostEntry[] };

export default function PostDetailPage() {
  const { postId } = useParams<{ postId: string }>();
  const q = useQuery({
    queryKey: ["post", postId],
    queryFn: () => api.get<Detail>(`/posts/${postId}`),
    enabled: !!postId,
  });

  if (q.isLoading) return <div className="p-6 text-fg-muted">Loading…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-danger-fg">Not found.</div>;

  const { post, snapshots, ghost_history } = q.data;

  return (
    <div className="p-4 sm:p-6 space-y-6 max-w-5xl">
      <div>
        <Link to="/posts" className="text-sm text-fg-muted hover:text-fg">
          ← All posts
        </Link>
      </div>

      <div className="rounded-lg border border-border bg-surface p-4 space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">{post.title || "(no title)"}</h1>
            <div className="text-sm text-fg-muted">
              {post.account} · posted {formatDateTime(post.posted_ts)}
            </div>
          </div>
        </div>
        {post.url && (
          <a
            href={post.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-info-fg hover:underline break-all"
          >
            {post.url}
          </a>
        )}
        <div className="text-xs text-fg-subtle">post_id: {post.post_id} · source: {post.source}</div>
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-fg-muted">Snapshot history</h2>
        {snapshots.length === 0 ? (
          <div className="text-fg-subtle text-sm">No snapshots yet.</div>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-surface text-fg-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Date</th>
                  <th className="px-3 py-2 text-left">Status</th>
                  <th className="px-3 py-2 text-right">Impressions</th>
                  <th className="px-3 py-2 text-right">Views</th>
                  <th className="px-3 py-2 text-right">Shares</th>
                  <th className="px-3 py-2 text-right">Favorites</th>
                  <th className="px-3 py-2 text-left">Note</th>
                </tr>
              </thead>
              <tbody>
                {snapshots.map((s) => (
                  <tr key={s.snapshot_date} className="border-t border-border">
                    <td className="px-3 py-2 whitespace-nowrap">{formatDate(s.snapshot_date)}</td>
                    <td className="px-3 py-2 text-fg-muted">{s.status || "—"}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatNumber(s.impressions)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatNumber(s.views)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatNumber(s.shares)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatNumber(s.favorites)}</td>
                    <td className="px-3 py-2 text-fg-subtle text-xs">{s.freshness_note || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-fg-muted">Ghost-check history</h2>
        {ghost_history.length === 0 ? (
          <div className="text-fg-subtle text-sm">Not checked yet.</div>
        ) : (
          <ul className="text-sm space-y-1">
            {ghost_history.map((g, i) => (
              <li key={i} className="flex items-center gap-3">
                <span className="text-fg-muted tabular-nums">{formatDateTime(g.ts)}</span>
                <span
                  className={
                    g.ghosted
                      ? "text-danger-fg text-xs px-1.5 py-0.5 rounded border border-danger-border bg-danger"
                      : "text-ok-fg text-xs px-1.5 py-0.5 rounded border border-ok-border bg-ok"
                  }
                >
                  {g.ghosted ? "ghosted" : "visible"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
