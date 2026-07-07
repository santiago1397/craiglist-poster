import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { formatDate, formatNumber, formatRate } from "../lib/format";
import { cn } from "../lib/cn";

type PostRow = {
  post_id: string;
  account: string;
  title: string | null;
  url: string | null;
  posted_ts: string | null;
  status: string | null;
  impressions: number | null;
  views: number | null;
  shares: number | null;
  favorites: number | null;
  ghosted: boolean | null;
  days_active: number | null;
  views_per_day: number | null;
  impressions_per_day: number | null;
};

type Resp = { total: number; limit: number; offset: number; items: PostRow[] };
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
      <h1 className="text-lg font-semibold">Posts</h1>

      <div className="grid gap-3 grid-cols-1 md:grid-cols-2 lg:grid-cols-6">
        <input
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          placeholder="Search title or post_id…"
          className="lg:col-span-2 rounded bg-slate-900 border border-slate-800 px-3 py-1.5 text-sm"
        />
        <select
          value={account}
          onChange={(e) => {
            setAccount(e.target.value);
            setPage(0);
          }}
          className="rounded bg-slate-900 border border-slate-800 px-2 py-1.5 text-sm"
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
          className="rounded bg-slate-900 border border-slate-800 px-2 py-1.5 text-sm"
        >
          <option value="">Any status</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
        </select>
        <select
          value={ghost}
          onChange={(e) => {
            setGhost(e.target.value);
            setPage(0);
          }}
          className="rounded bg-slate-900 border border-slate-800 px-2 py-1.5 text-sm"
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
          className="rounded bg-slate-900 border border-slate-800 px-2 py-1.5 text-sm"
        >
          <option value="">Last 90 days</option>
          <option value="all">All time</option>
        </select>
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900 text-slate-300">
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
              <tr key={r.post_id} className="border-t border-slate-800 hover:bg-slate-900/50">
                <td className="px-3 py-2 text-slate-300">{r.account}</td>
                <td className="px-3 py-2 max-w-xs">
                  <Link
                    to={`/posts/${r.post_id}`}
                    className="text-slate-100 hover:underline block truncate"
                    title={r.title || ""}
                  >
                    {r.title || <span className="text-slate-500">(no title)</span>}
                  </Link>
                  <div className="text-xs text-slate-500">{r.post_id}</div>
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-slate-400">
                  {formatDate(r.posted_ts)}
                </td>
                <td className="px-3 py-2">
                  <StatusChip status={r.status} ghosted={r.ghosted} />
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{formatNumber(r.impressions)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{formatNumber(r.views)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {formatRate(r.impressions_per_day)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {formatRate(r.views_per_day)}
                </td>
                <td className="px-3 py-2">
                  {r.url && (
                    <a
                      href={r.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-blue-400 hover:underline"
                      title="Open on Craigslist"
                    >
                      ↗
                    </a>
                  )}
                </td>
              </tr>
            ))}
            {items.length === 0 && !q.isLoading && (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-slate-500">
                  No posts match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-slate-400">
        <div>
          {q.isLoading ? "Loading…" : `${formatNumber(total)} posts`}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage(Math.max(0, page - 1))}
            disabled={page === 0}
            className="px-2 py-1 rounded border border-slate-800 hover:bg-slate-900 disabled:opacity-40"
          >
            Prev
          </button>
          <span>
            Page {page + 1} / {maxPage + 1}
          </span>
          <button
            onClick={() => setPage(Math.min(maxPage, page + 1))}
            disabled={page >= maxPage}
            className="px-2 py-1 rounded border border-slate-800 hover:bg-slate-900 disabled:opacity-40"
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
        className={cn("inline-flex items-center gap-1 hover:text-white", active ? "text-white" : "")}
      >
        {label}
        {active && <span className="text-xs">{dir === "asc" ? "▲" : "▼"}</span>}
      </button>
    </th>
  );
}

function StatusChip({ status, ghosted }: { status: string | null; ghosted: boolean | null }) {
  if (ghosted === true) {
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-red-950 text-red-300 border border-red-800">
        ghosted
      </span>
    );
  }
  const active = status === "Active";
  return (
    <span
      className={cn(
        "inline-flex items-center px-1.5 py-0.5 rounded text-xs border",
        active
          ? "bg-emerald-950 text-emerald-300 border-emerald-800"
          : "bg-slate-800 text-slate-400 border-slate-700",
      )}
    >
      {status || "unknown"}
    </span>
  );
}
