// Review module (phase 1).
//
// One screen for everything needing attention: unreviewed drafts, drafts parked
// after a failed post, and queue health. Review is triage, not a gate — drafts
// post whether or not they have been read (decision 22), so this page's job is
// to make it easy to catch one before it goes rather than to hold it back.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";

type Draft = {
  id: number;
  account: string;
  status: string;
  position: number;
  reviewed: boolean;
  title: string;
  body: string;
  body_head: string | null;
  city: string;
  county: string;
  postal_code: string;
  phone_number: string;
  not_before: string | null;
  expires_at: string | null;
  failed_step: string | null;
  failed_message: string | null;
  attempts: number;
  created_at: string;
};

type AccountHealth = {
  eligible: boolean;
  reasons: string[];
  last_post_at: string | null;
  posts_last_7d: number;
  queue_depth: number;
};

type Health = {
  global_blocks: string[];
  posts_last_24h_total: number;
  accounts: Record<string, AccountHealth>;
  unreviewed: number;
  needs_attention: number;
};

const FILTERS = [
  { key: "needs_attention", label: "Needs attention" },
  { key: "unreviewed", label: "Unreviewed" },
  { key: "queued", label: "Queue" },
  { key: "all", label: "All" },
] as const;

type FilterKey = (typeof FILTERS)[number]["key"];

function fmt(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function ReviewPage() {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [filter, setFilter] = useState<FilterKey>("needs_attention");
  const [accounts, setAccounts] = useState<string[]>([]);
  const [editing, setEditing] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const params = useMemo(() => {
    switch (filter) {
      case "unreviewed":
        return { status: "queued", reviewed: "false" };
      case "queued":
        return { status: "queued" };
      case "needs_attention":
        return { status: "needs_attention" };
      default:
        return {};
    }
  }, [filter]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [list, accts] = await Promise.all([
        api.get<{ drafts: Draft[] }>("/drafts", params),
        api.get<{ accounts: string[] }>("/accounts"),
      ]);
      setDrafts(list.drafts);
      setAccounts(accts.accounts);
      if (accts.accounts.length) {
        setHealth(await api.get<Health>("/drafts/health", { accounts: accts.accounts.join(",") }));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [params]);

  useEffect(() => {
    void load();
  }, [load]);

  async function mutate(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 space-y-4">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Review</h1>
        <button
          onClick={() => void load()}
          className="text-sm px-2 py-1 rounded hover:bg-slate-800 text-slate-300"
        >
          Refresh
        </button>
      </header>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      {health && <QueueHealth health={health} accounts={accounts} />}

      <div className="flex gap-1">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={cn(
              "px-3 py-1.5 rounded text-sm",
              filter === f.key ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-800/60",
            )}
          >
            {f.label}
            {f.key === "needs_attention" && health?.needs_attention
              ? ` (${health.needs_attention})`
              : null}
            {f.key === "unreviewed" && health?.unreviewed ? ` (${health.unreviewed})` : null}
          </button>
        ))}
      </div>

      {drafts.length === 0 ? (
        <p className="text-slate-500 text-sm py-8 text-center">
          Nothing here.
          {filter === "queued" && " An empty queue means nothing will post — posting is fail-closed."}
        </p>
      ) : (
        <ul className="space-y-2">
          {drafts.map((d) => (
            <DraftRow
              key={d.id}
              draft={d}
              busy={busy}
              onEdit={() => setEditing(d)}
              onReview={() => mutate(() => api.patch(`/drafts/${d.id}`, { reviewed: !d.reviewed }))}
              onTop={() => mutate(() => api.post(`/drafts/${d.id}/reorder`, { after_id: null }))}
              onRequeue={() => mutate(() => api.post(`/drafts/${d.id}/requeue`))}
              onDelete={() => mutate(() => api.del(`/drafts/${d.id}`))}
            />
          ))}
        </ul>
      )}

      {editing && (
        <EditDialog
          draft={editing}
          onClose={() => setEditing(null)}
          onSave={async (patch) => {
            await mutate(() => api.patch(`/drafts/${editing.id}`, patch));
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}

function QueueHealth({ health, accounts }: { health: Health; accounts: string[] }) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/50 p-3 space-y-2">
      {health.global_blocks.length > 0 && (
        <p className="text-sm text-amber-300">
          Nothing can post right now: {health.global_blocks.join("; ")}
        </p>
      )}
      <div className="grid gap-2 sm:grid-cols-3">
        {accounts.map((name) => {
          const a = health.accounts[name];
          if (!a) return null;
          return (
            <div key={name} className="rounded bg-slate-900 border border-slate-800 p-2">
              <div className="flex items-center justify-between">
                <span className="font-medium text-sm">{name}</span>
                <span
                  className={cn(
                    "text-xs px-1.5 py-0.5 rounded",
                    a.queue_depth === 0
                      ? "bg-red-900/60 text-red-200"
                      : "bg-slate-800 text-slate-300",
                  )}
                >
                  {a.queue_depth} queued
                </span>
              </div>
              <p className="text-xs text-slate-500 mt-1">last post {fmt(a.last_post_at)}</p>
              {!a.eligible && (
                <p className="text-xs text-slate-400 mt-1">{a.reasons.join("; ")}</p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DraftRow(props: {
  draft: Draft;
  busy: boolean;
  onEdit: () => void;
  onReview: () => void;
  onTop: () => void;
  onRequeue: () => void;
  onDelete: () => void;
}) {
  const { draft: d, busy } = props;
  const parked = d.status === "needs_attention";
  return (
    <li
      className={cn(
        "rounded border p-3",
        parked ? "border-amber-800/70 bg-amber-950/20" : "border-slate-800 bg-slate-900/40",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-slate-500">#{d.id}</span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">
              {d.account}
            </span>
            {!d.reviewed && d.status === "queued" && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-sky-900/60 text-sky-200">
                unreviewed
              </span>
            )}
            {parked && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-200">
                failed at {d.failed_step ?? "unknown step"}
              </span>
            )}
          </div>
          <p className="mt-1 truncate font-medium">{d.title}</p>
          <p className="text-xs text-slate-500 mt-0.5">
            {d.city}
            {d.postal_code ? ` ${d.postal_code}` : ""} · created {fmt(d.created_at)}
            {d.attempts > 1 ? ` · ${d.attempts} attempts` : ""}
          </p>
          {parked && d.failed_message && (
            <p className="text-xs text-amber-200/80 mt-1 line-clamp-2">{d.failed_message}</p>
          )}
          {parked && (
            <p className="text-xs text-slate-400 mt-1">
              Images were already sent to Craigslist on this attempt. Check the listing did not
              publish before requeueing.
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-1">
          <Action label="Edit" onClick={props.onEdit} busy={busy} />
          {d.status === "queued" && (
            <>
              <Action label="Top" onClick={props.onTop} busy={busy} />
              <Action label={d.reviewed ? "Unmark" : "Reviewed"} onClick={props.onReview} busy={busy} />
            </>
          )}
          {parked && <Action label="Requeue" onClick={props.onRequeue} busy={busy} />}
          <Action label="Delete" onClick={props.onDelete} busy={busy} danger />
        </div>
      </div>
    </li>
  );
}

function Action(props: { label: string; onClick: () => void; busy: boolean; danger?: boolean }) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.busy}
      className={cn(
        "text-xs px-2 py-1 rounded border disabled:opacity-40",
        props.danger
          ? "border-red-900 text-red-300 hover:bg-red-950/50"
          : "border-slate-700 text-slate-300 hover:bg-slate-800",
      )}
    >
      {props.label}
    </button>
  );
}

function EditDialog(props: {
  draft: Draft;
  onClose: () => void;
  onSave: (patch: Record<string, unknown>) => Promise<void>;
}) {
  const [title, setTitle] = useState(props.draft.title);
  const [body, setBody] = useState(props.draft.body);

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-4 z-10">
      <div className="bg-slate-900 border border-slate-700 rounded-lg w-full max-w-3xl max-h-[90vh] flex flex-col">
        <div className="p-3 border-b border-slate-800 flex items-center justify-between">
          <h2 className="font-medium">Edit draft #{props.draft.id}</h2>
          <button onClick={props.onClose} className="text-slate-400 hover:text-white px-2">
            ✕
          </button>
        </div>
        <div className="p-3 space-y-3 overflow-auto">
          <label className="block">
            <span className="text-xs text-slate-400">Title</span>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs text-slate-400">
              Body — the keyword tail is part of this text; edit the top section
            </span>
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={16}
              className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm font-mono"
            />
          </label>
        </div>
        <div className="p-3 border-t border-slate-800 flex justify-end gap-2">
          <button
            onClick={props.onClose}
            className="px-3 py-1.5 rounded text-sm text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            onClick={() => void props.onSave({ title, body, reviewed: true })}
            className="px-3 py-1.5 rounded text-sm bg-sky-700 hover:bg-sky-600"
          >
            Save &amp; mark reviewed
          </button>
        </div>
      </div>
    </div>
  );
}
