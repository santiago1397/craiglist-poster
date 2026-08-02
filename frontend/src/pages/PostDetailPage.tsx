import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { formatDate, formatDateTime, formatNumber } from "../lib/format";
import { PostEditPanel } from "../components/post/PostEditPanel";
import { PostEditHistory } from "../components/post/PostEditHistory";
import { PostRecovery } from "../components/post/PostRecovery";
import { PublishedImages } from "../components/post/PublishedImages";
import type { LocationRef } from "../lib/posting";
import type { EditablePost } from "../lib/edits";

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
  const [error, setError] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["post", postId],
    queryFn: () => api.get<Detail>(`/posts/${postId}`),
    enabled: !!postId,
  });

  // The editing half. Polls only while something is actually in flight — a
  // detail page that refetches every 15 seconds forever is noise, but a
  // hydration you are sat waiting for needs to arrive on its own.
  const editQ = useQuery({
    queryKey: ["edits", postId],
    queryFn: () => api.get<EditablePost>(`/edits/${postId}`),
    enabled: !!postId,
    // Poll while anything is in flight — including the window between asking
    // for a reconcile and the desktop claiming it. Keying only on `applying`
    // was circular: the page learns it is applying by polling, and only starts
    // polling once it knows. So Apply now left a stale view in which the
    // controls stayed enabled, and the next click came back 409 "being edited
    // right now" with no visible reason.
    refetchInterval: (query) => {
      const d = query.state.data;
      if (!d) return false;
      return d.hydrate_requested_at || d.reconcile_requested_at ||
        d.edit_status === "applying"
        ? 5_000
        : false;
    },
  });

  // Both feed the form's selects. Same query keys Review uses, so arriving from
  // that page costs nothing.
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<{ accounts: string[] }>("/accounts"),
    staleTime: 5 * 60_000,
  });
  const locationsQ = useQuery({
    queryKey: ["reference", "locations"],
    queryFn: () => api.get<LocationRef>("/reference/locations"),
    staleTime: Infinity,
  });

  if (q.isLoading) return <div className="p-6 text-fg-muted">Loading…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-danger-fg">Not found.</div>;

  const { post, snapshots, ghost_history } = q.data;
  const editable = editQ.data;

  return (
    <div className="p-4 sm:p-6 space-y-6 max-w-5xl">
      <div>
        <Link to="/posts" className="text-sm text-fg-muted hover:text-fg">
          ← All posts
        </Link>
      </div>

      {error && (
        <div className="rounded border border-danger-border bg-danger/10 px-3 py-2 text-sm text-danger-fg">
          {error}{" "}
          <button onClick={() => setError(null)} className="underline">
            dismiss
          </button>
        </div>
      )}


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

      {/* Recovery first: a degraded live posting is the one thing on this page
          that cannot wait, and burying it under the editor would be wrong. */}
      {editable && <PostRecovery post={editable} onError={setError} />}

      {editQ.isError ? (
        <p className="text-sm text-fg-subtle">
          Could not load this post's editing state.
        </p>
      ) : editable ? (
        <PostEditPanel
          post={editable}
          accounts={accountsQ.data?.accounts ?? []}
          locations={locationsQ.data ?? null}
          onError={setError}
        />
      ) : null}

      {editable && <PublishedImages images={editable.published_images ?? []} />}

      {editable && <PostEditHistory attempts={editable.attempts ?? []} />}

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
