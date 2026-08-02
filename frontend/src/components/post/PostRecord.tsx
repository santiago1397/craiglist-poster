// What survives of a posting whose copy we never captured.
//
// The dashboard did not store ad copy until 2026-07-30. Everything posted
// before that exists only as the row Craigslist's own account list gave us: a
// title, a URL, and a daily stats reading. The body and the pictures are not
// held anywhere.
//
// Left alone, the detail page renders that as absence — an empty editor, no
// gallery, "Never loaded from Craigslist" — which reads as a page that failed
// to load rather than a posting whose copy is genuinely gone. And the highest-
// performing ad in the whole system is one of these: 1,402 impressions on a
// posting from 17 June whose copy we do not have.
//
// So state it. What we know, what we do not, and the one thing that would still
// recover it.

import { formatDate, formatDateTime, formatNumber } from "../../lib/format";

type Snapshot = {
  snapshot_date: string;
  status: string | null;
  impressions: number | null;
  views: number | null;
  area: string | null;
  category: string | null;
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-sm">
      <dt className="text-fg-muted w-32 shrink-0">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

export function PostRecord({
  post,
  snapshots,
  hasCopy,
  hasImages,
}: {
  post: { post_id: string; account: string; title: string | null; url: string | null;
          posted_ts: string | null; source: string | null };
  snapshots: Snapshot[];
  hasCopy: boolean;
  hasImages: boolean;
}) {
  // Only for postings we hold nothing of. Once the copy is recovered the
  // ordinary editor and gallery say everything this would.
  if (hasCopy && hasImages) return null;

  // Impressions are cumulative on Craigslist's side, so the last reading is the
  // total — but take the max rather than the last row, because a scrape that
  // failed mid-run can write a lower number afterwards.
  const peak = snapshots.reduce(
    (a, s) => ({
      impressions: Math.max(a.impressions, s.impressions ?? 0),
      views: Math.max(a.views, s.views ?? 0),
    }),
    { impressions: 0, views: 0 },
  );
  const dated = snapshots.filter((s) => s.snapshot_date);
  const first = dated[0];
  const last = dated[dated.length - 1];
  const placed = snapshots.find((s) => s.area || s.category);

  return (
    <section className="rounded-lg border border-warn-border bg-warn/10 p-4 space-y-3">
      <div>
        <h2 className="font-semibold">What we still have of this ad</h2>
        <p className="text-xs text-fg-muted">
          {!hasCopy && !hasImages
            ? "The wording and the pictures were never stored."
            : !hasCopy
              ? "The wording was never stored."
              : "The pictures were never stored."}{" "}
          Everything below is kept for good — it is not fetched from Craigslist
          when you open this page.
        </p>
      </div>

      <dl className="space-y-1.5">
        <Row label="Title">
          {post.title ? (
            <span className="font-medium">{post.title}</span>
          ) : (
            <span className="text-fg-subtle">not recorded</span>
          )}
        </Row>
        <Row label="Posted">
          {formatDateTime(post.posted_ts)} · {post.account}
        </Row>
        {placed && (
          <Row label="Placed in">
            {placed.area || "—"}
            {placed.category ? ` · ${placed.category}` : ""}
          </Row>
        )}
        {peak.impressions > 0 && (
          <Row label="Performance">
            <span className="font-medium tabular-nums">
              {formatNumber(peak.impressions)}
            </span>{" "}
            impressions ·{" "}
            <span className="tabular-nums">{formatNumber(peak.views)}</span> views
          </Row>
        )}
        {first && last && (
          <Row label="Tracked">
            {formatDate(first.snapshot_date)} → {formatDate(last.snapshot_date)}
            <span className="text-fg-subtle">
              {" "}
              ({snapshots.length} daily reading{snapshots.length === 1 ? "" : "s"})
            </span>
          </Row>
        )}
        {post.url && (
          <Row label="Craigslist URL">
            {/* Not a link. These 404 once the ad ends, and offering a link that
                goes nowhere is worse than showing the address it lived at. */}
            <code className="text-xs break-all text-fg-muted">{post.url}</code>
          </Row>
        )}
        <Row label="Post id">
          <code className="text-xs">{post.post_id}</code>
          {post.source && <span className="text-fg-subtle"> · via {post.source}</span>}
        </Row>
      </dl>

      <p className="text-xs text-fg-subtle border-t border-warn-border pt-2">
        One thing can still recover this: Craigslist keeps finished postings on
        the account's own <strong>inactive</strong> list for a while, and{" "}
        <code>cl scan-ended</code> on the posting machine reads them. If it is no
        longer listed there, the record above is all that will ever exist — the
        public URL stops resolving as soon as the ad ends.
      </p>
    </section>
  );
}
