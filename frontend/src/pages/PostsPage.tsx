import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, Circle, CircleOff, ExternalLink, EyeOff, HelpCircle } from "lucide-react";
import { api } from "../lib/api";
import { formatDate, formatNumber, formatRate } from "../lib/format";
import { cn } from "../lib/cn";

type Liveness = "live" | "ended" | "unknown";

type PostRow = {
  post_id: string;
  account: string;
  title: string | null;
  url: string | null;
  posted_ts: string | null;
  status: string | null;
  liveness: Liveness;
  snapshot_date: string | null;
  last_sync_date: string | null;
  sync_age_days: number | null;
  impressions: number | null;
  views: number | null;
  shares: number | null;
  favorites: number | null;
  ghosted: boolean | null;
  days_active: number | null;
  views_per_day: number | null;
  impressions_per_day: number | null;
};

type SyncAccount = {
  account: string;
  last_sync_date: string | null;
  age_days: number | null;
  stale: boolean;
};
type SyncInfo = { accounts: SyncAccount[]; stale: boolean; stale_after_days: number };

type Resp = {
  total: number;
  limit: number;
  offset: number;
  items: PostRow[];
  counts: Partial<Record<Liveness, number>>;
  sync: SyncInfo;
};
type AccountsResp = { accounts: string[] };

const PAGE_SIZE = 50;

export default function PostsPage() {
  const [account, setAccount] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [ghost, setGhost] = useState<string>("");
  const [since, setSince] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [sort, setSort] = useState<string>("posted_ts");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<AccountsResp>("/accounts"),
    staleTime: 5 * 60_000,
  });

  const params = useMemo(
    () => ({
      account: account || undefined,
      status: status || undefined,
      ghost: ghost || undefined,
      since: since || undefined,
      search: search || undefined,
      sort,
      sort_dir: sortDir,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    [account, status, ghost, since, search, sort, sortDir, page],
  );

  const q = useQuery({
    queryKey: ["posts", params],
    queryFn: () => api.get<Resp>("/posts", params as any),
    placeholderData: (prev) => prev,
  });

  const items = q.data?.items ?? [];
  const total = q.data?.total ?? 0;
  const counts = q.data?.counts ?? {};
  const sync = q.data?.sync;
  const maxPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  const clickSort = (key: string) => {
    if (sort === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSort(key);
      setSortDir("desc");
    }
    setPage(0);
  };

  return (
    <div className="p-4 sm:p-6 space-y-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-lg font-semibold">Posts</h1>
        <p className="text-sm text-fg-muted">
          <span className="text-ok-fg">{formatNumber(counts.live ?? 0)} live</span>
          {" · "}
          {formatNumber(counts.ended ?? 0)} ended
          {counts.unknown ? ` · ${formatNumber(counts.unknown)} never checked` : null}
        </p>
      </div>

      {sync?.stale && <StaleSyncBanner sync={sync} />}

      <div className="grid gap-3 grid-cols-1 md:grid-cols-2 lg:grid-cols-6">
        <input
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          placeholder="Search title or post_id…"
          className="lg:col-span-2 rounded bg-surface border border-border px-3 py-1.5 text-sm"
        />
        <select
          value={account}
          onChange={(e) => {
            setAccount(e.target.value);
            setPage(0);
          }}
          className="rounded bg-surface border border-border px-2 py-1.5 text-sm"
        >
          <option value="">All accounts</option>
          {(accountsQ.data?.accounts || []).map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setPage(0);
          }}
          className="rounded bg-surface border border-border px-2 py-1.5 text-sm"
        >
          <option value="">Live and ended</option>
          <option value="active">Live only</option>
          <option value="inactive">Ended only</option>
          <option value="unknown">Never checked</option>
        </select>
        <select
          value={ghost}
          onChange={(e) => {
            setGhost(e.target.value);
            setPage(0);
          }}
          className="rounded bg-surface border border-border px-2 py-1.5 text-sm"
        >
          <option value="">Any ghost state</option>
          <option value="visible">Visible</option>
          <option value="ghosted">Ghosted</option>
          <option value="unchecked">Unchecked</option>
        </select>
        <select
          value={since}
          onChange={(e) => {
            setSince(e.target.value);
            setPage(0);
          }}
          className="rounded bg-surface border border-border px-2 py-1.5 text-sm"
        >
          <option value="">Last 90 days</option>
          <option value="all">All time</option>
        </select>
      </div>

      {/* Below md the nine-column table becomes cards. It was in an
          overflow-x-auto box, so it did not scroll the page — but swiping a
          nine-column table sideways on a 390px screen is not usable either.
          Sorting moves to a select, since there are no column headers to tap. */}
      <div className="md:hidden flex items-center gap-2">
        <label className="text-xs text-fg-muted" htmlFor="mobile-sort">
          Sort by
        </label>
        <select
          id="mobile-sort"
          value={`${sort}:${sortDir}`}
          onChange={(e) => {
            const [k, dir] = e.target.value.split(":");
            setSort(k);
            setSortDir(dir as "asc" | "desc");
            setPage(0);
          }}
          className="flex-1 rounded bg-surface border border-border px-2 py-1.5 text-sm"
        >
          <option value="posted_ts:desc">Newest first</option>
          <option value="posted_ts:asc">Oldest first</option>
          <option value="impressions:desc">Most impressions</option>
          <option value="views:desc">Most views</option>
          <option value="impressions_per_day:desc">Best impressions/day</option>
          <option value="views_per_day:desc">Best views/day</option>
        </select>
      </div>

      <ul className="md:hidden space-y-2">
        {items.map((r) => (
          <li key={r.post_id} className="rounded-lg border border-border bg-surface/40 p-3">
            <div className="flex items-start justify-between gap-2">
              <Link to={`/posts/${r.post_id}`} className="min-w-0 flex-1">
                <p className="font-medium line-clamp-2">
                  {r.title || <span className="text-fg-subtle">(no title)</span>}
                </p>
                <p className="text-xs text-fg-subtle mt-0.5">
                  {r.account} · {formatDate(r.posted_ts)} · {r.post_id}
                </p>
              </Link>
              <StatusChip liveness={r.liveness} ghosted={r.ghosted} staleDays={r.sync_age_days} />
            </div>
            <dl className="mt-2 grid grid-cols-4 gap-2 text-center">
              {[
                ["Impr", formatNumber(r.impressions)],
                ["Views", formatNumber(r.views)],
                ["Impr/d", formatRate(r.impressions_per_day)],
                ["Views/d", formatRate(r.views_per_day)],
              ].map(([label, value]) => (
                <div key={label} className="rounded bg-surface-2/60 py-1">
                  <dt className="text-[11px] text-fg-subtle">{label}</dt>
                  <dd className="text-sm tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
            {r.url && (
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 inline-flex items-center gap-1 text-xs text-info-fg hover:underline"
              >
                <ExternalLink size={12} aria-hidden="true" />
                Open on Craigslist
              </a>
            )}
          </li>
        ))}
        {items.length === 0 && !q.isLoading && (
          <li className="text-center text-fg-subtle text-sm py-6">No posts match.</li>
        )}
      </ul>

      <div className="hidden md:block overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-surface text-fg-muted">
            <tr>
              <th className="px-3 py-2 text-left">Account</th>
              <th className="px-3 py-2 text-left">Title</th>
              <SortHeader label="Posted" k="posted_ts" cur={sort} dir={sortDir} onClick={clickSort} />
              <th className="px-3 py-2 text-left">Status</th>
              <SortHeader label="Impr" k="impressions" cur={sort} dir={sortDir} onClick={clickSort} />
              <SortHeader label="Views" k="views" cur={sort} dir={sortDir} onClick={clickSort} />
              <SortHeader label="Impr/d" k="impressions_per_day" cur={sort} dir={sortDir} onClick={clickSort} />
              <SortHeader label="Views/d" k="views_per_day" cur={sort} dir={sortDir} onClick={clickSort} />
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.post_id} className="border-t border-border hover:bg-surface/50">
                <td className="px-3 py-2 text-fg-muted">{r.account}</td>
                <td className="px-3 py-2 max-w-xs">
                  <Link
                    to={`/posts/${r.post_id}`}
                    className="text-fg hover:underline block truncate"
                    title={r.title || ""}
                  >
                    {r.title || <span className="text-fg-subtle">(no title)</span>}
                  </Link>
                  <div className="text-xs text-fg-subtle">{r.post_id}</div>
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-fg-muted">
                  {formatDate(r.posted_ts)}
                </td>
                <td className="px-3 py-2">
                  <StatusChip
                  liveness={r.liveness}
                  ghosted={r.ghosted}
                  staleDays={r.sync_age_days}
                />
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{formatNumber(r.impressions)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{formatNumber(r.views)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-fg-muted">
                  {formatRate(r.impressions_per_day)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-fg-muted">
                  {formatRate(r.views_per_day)}
                </td>
                <td className="px-3 py-2">
                  {r.url && (
                    <a
                      href={r.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex text-info-fg hover:underline"
                      // "↗" alone is announced as punctuation by a screen
                      // reader and gives no hint of where it goes.
                      aria-label={`Open "${r.title ?? r.post_id}" on Craigslist in a new tab`}
                      title="Open on Craigslist"
                    >
                      <ExternalLink size={14} aria-hidden="true" />
                    </a>
                  )}
                </td>
              </tr>
            ))}
            {items.length === 0 && !q.isLoading && (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-fg-subtle">
                  No posts match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-fg-muted">
        <div>
          {q.isLoading ? "Loading…" : `${formatNumber(total)} posts`}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage(Math.max(0, page - 1))}
            disabled={page === 0}
            className="px-2 py-1 rounded border border-border hover:bg-surface disabled:opacity-40"
          >
            Prev
          </button>
          <span>
            Page {page + 1} / {maxPage + 1}
          </span>
          <button
            onClick={() => setPage(Math.min(maxPage, page + 1))}
            disabled={page >= maxPage}
            className="px-2 py-1 rounded border border-border hover:bg-surface disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}

function SortHeader({
  label,
  k,
  cur,
  dir,
  onClick,
}: {
  label: string;
  k: string;
  cur: string;
  dir: "asc" | "desc";
  onClick: (k: string) => void;
}) {
  const active = cur === k;
  return (
    <th className="px-3 py-2 text-right">
      <button
        onClick={() => onClick(k)}
        className={cn("inline-flex items-center gap-1 hover:text-fg", active ? "text-fg" : "")}
      >
        {label}
        {active && <span className="text-xs">{dir === "asc" ? "▲" : "▼"}</span>}
      </button>
    </th>
  );
}

function StaleSyncBanner({ sync }: { sync: SyncInfo }) {
  const worst = [...sync.accounts].sort((a, b) => (b.age_days ?? 0) - (a.age_days ?? 0))[0];
  return (
    <div className="flex items-start gap-2 rounded-lg border border-warn-border bg-warn px-3 py-2 text-sm text-warn-fg">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
      <div>
        <p className="font-medium">
          Stats are {worst?.age_days ?? "?"} days stale — nothing below is confirmed current.
        </p>
        <p className="text-xs opacity-90 mt-0.5">
          The daily scrape last ran{" "}
          {sync.accounts.map((a) => `${a.account}: ${a.last_sync_date ?? "never"}`).join(" · ")}.
          Until it runs again, "live" only means the post was up on that date.
        </p>
      </div>
    </div>
  );
}

const LIVENESS_STYLE: Record<Liveness, { label: string; cls: string; Icon: typeof Circle }> = {
  live: { label: "live", cls: "bg-ok text-ok-fg border-ok-border", Icon: Circle },
  ended: {
    label: "ended",
    cls: "bg-surface-2 text-fg-muted border-border-strong",
    Icon: CircleOff,
  },
  unknown: {
    label: "not checked",
    cls: "bg-surface-2 text-fg-subtle border-border",
    Icon: HelpCircle,
  },
};

function StatusChip({
  liveness,
  ghosted,
  staleDays,
}: {
  liveness: Liveness;
  ghosted: boolean | null;
  staleDays: number | null;
}) {
  if (ghosted === true) {
    return (
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-danger text-danger-fg border border-danger-border">
        <EyeOff size={12} aria-hidden="true" />
        ghosted
      </span>
    );
  }
  // Reads the derived `liveness`, not Craigslist's raw status string. The raw
  // one arrives as both "Active" and "active", and — more importantly — it is
  // whatever the last scrape saw, so it keeps asserting "Active" forever once
  // the scrape stops. A post that has dropped off the active tab has no row at
  // all, which is an absence only the server-side derivation can read.
  const { label, cls, Icon } = LIVENESS_STYLE[liveness] ?? LIVENESS_STYLE.unknown;
  // A "live" badge backed by a week-old scrape is qualified rather than shown
  // plain, so staleness is visible on the row and not only in the banner.
  const unconfirmed = liveness === "live" && staleDays != null && staleDays > 2;
  return (
    <span
      className={cn("inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs border", cls)}
      title={unconfirmed ? `Last confirmed ${staleDays} days ago` : undefined}
    >
      {/* An icon as well as a colour: red-vs-green alone excludes the ~8% of
          men with red-green colour blindness. */}
      <Icon size={12} aria-hidden="true" />
      {label}
      {unconfirmed && <span className="opacity-70">({staleDays}d ago)</span>}
    </span>
  );
}
