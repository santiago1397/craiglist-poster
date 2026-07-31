// Editing live postings.
//
// A post is write-once until it appears here. The desktop reads its real
// content off Craigslist ("Load"), you change what it should say, and the
// desktop reconciles the live posting to match next time it can take the
// browser lease — which may be hours, so nothing here is synchronous.
//
// Two states deserve your attention and sort to the top: DEGRADED means a
// reconcile mutated a live post and could not finish, and PARKED means the
// post moved underneath an edit and we refused to clobber it.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, Loader2 } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { Modal } from "../components/Modal";

type Post = {
  post_id: string;
  account: string;
  title: string | null;
  url: string | null;
  posted_ts: string | null;
  body: string | null;
  city: string | null;
  county: string | null;
  postal_code: string | null;
  phone_number: string | null;
  license_number: string | null;
  service_offered: string | null;
  live_status: string | null;
  editable: boolean;
  hydrated_at: string | null;
  hydrate_requested_at: string | null;
  hydrate_error: string | null;
  edit_status: string | null;
  desired_rev: number | null;
  live_rev: number | null;
  failed_step: string | null;
  failed_message: string | null;
  has_pending_edit: boolean;
};

type Health = {
  pending: number;
  applying: number;
  degraded: number;
  parked: number;
  hydrating: number;
  global_blocks: string[];
  accounts: Record<string, { eligible: boolean; reasons: string[] }>;
};

type Attempt = {
  event_id: string;
  ts: string;
  outcome: string;
  failed_step: string | null;
  error_message: string | null;
  duration_seconds: number | null;
  steps: { name: string; ok: boolean; duration_seconds: number | null; note: string | null }[];
  artifact_ids: string[];
};

const TABS = [
  { key: "degraded", label: "Degraded" },
  { key: "parked", label: "Parked" },
  { key: "pending", label: "Pending" },
  { key: "", label: "All posts" },
] as const;

const POLL = 15_000;

function fmt(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function EditsPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<string>("degraded");
  const [editing, setEditing] = useState<Post | null>(null);
  const [viewing, setViewing] = useState<Post | null>(null);
  const [error, setError] = useState<string | null>(null);

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<{ accounts: string[] }>("/accounts"),
    staleTime: 5 * 60_000,
  });

  const list = useQuery({
    queryKey: ["edits", tab],
    queryFn: () => api.get<{ posts: Post[]; total: number }>("/edits", { status: tab }),
    refetchInterval: POLL,
  });

  const health = useQuery({
    queryKey: ["edits", "health", accounts.data?.accounts],
    queryFn: () =>
      api.get<Health>("/edits/health", { accounts: (accounts.data?.accounts ?? []).join(",") }),
    enabled: (accounts.data?.accounts ?? []).length > 0,
    refetchInterval: POLL,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["edits"] });

  const mutate = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: invalidate,
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  const posts = list.data?.posts ?? [];

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <header className="mb-5">
        <h1 className="text-xl font-semibold">Edit live postings</h1>
        <p className="mt-1 text-sm text-fg-muted">
          Changes are applied by the desktop when it next has a free browser —
          not immediately.
        </p>
      </header>

      {health.data && <HealthBar health={health.data} />}

      {error && (
        <div className="mb-4 rounded border border-danger-border bg-danger px-3 py-2 text-sm text-danger-fg">
          {error}
          <button className="ml-3 underline" onClick={() => setError(null)}>dismiss</button>
        </div>
      )}

      <div className="mb-4 flex flex-wrap gap-1">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              "rounded px-3 py-1.5 text-sm",
              tab === t.key ? "bg-accent text-accent-fg" : "text-fg-muted hover:bg-surface",
            )}
          >
            {t.label}
            {t.key === "degraded" && health.data?.degraded ? ` (${health.data.degraded})` : ""}
            {t.key === "parked" && health.data?.parked ? ` (${health.data.parked})` : ""}
          </button>
        ))}
      </div>

      {list.isLoading && <p className="text-sm text-fg-muted">Loading…</p>}
      {!list.isLoading && posts.length === 0 && (
        <p className="rounded border border-border bg-surface px-4 py-6 text-sm text-fg-muted">
          Nothing here.
        </p>
      )}

      <ul className="space-y-2">
        {posts.map((p) => (
          <PostRow
            key={p.post_id}
            post={p}
            busy={mutate.isPending}
            onHydrate={() => mutate.mutate(() => api.post(`/edits/${p.post_id}/hydrate`))}
            onEdit={() => setEditing(p)}
            onHistory={() => setViewing(p)}
            onRequeue={() => mutate.mutate(() => api.post(`/edits/${p.post_id}/requeue`))}
            onDiscard={() => mutate.mutate(() => api.del(`/edits/${p.post_id}/desired`))}
          />
        ))}
      </ul>

      {editing && (
        <EditDialog
          post={editing}
          onClose={() => setEditing(null)}
          onSave={async (patch) => {
            await mutate.mutateAsync(() => api.put(`/edits/${editing.post_id}/desired`, patch));
            setEditing(null);
          }}
        />
      )}
      {viewing && <HistoryDialog post={viewing} onClose={() => setViewing(null)} />}
    </div>
  );
}

function HealthBar({ health }: { health: Health }) {
  return (
    <div className="mb-4 rounded border border-border bg-surface p-3">
      <div className="flex flex-wrap gap-4 text-sm">
        <Stat label="Pending" value={health.pending} />
        <Stat label="Applying" value={health.applying} />
        <Stat label="Hydrating" value={health.hydrating} />
        <Stat label="Parked" value={health.parked} tone={health.parked ? "warn" : undefined} />
        <Stat label="Degraded" value={health.degraded} tone={health.degraded ? "danger" : undefined} />
      </div>
      {health.global_blocks.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-warn-fg">
          {health.global_blocks.map((b) => (
            <li key={b}>• {b}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "warn" | "danger" }) {
  return (
    <div>
      <span className="text-fg-subtle">{label} </span>
      <span
        className={cn(
          "font-medium",
          tone === "danger" && "text-danger-fg",
          tone === "warn" && "text-warn-fg",
        )}
      >
        {value}
      </span>
    </div>
  );
}

function PostRow(props: {
  post: Post;
  busy: boolean;
  onHydrate: () => void;
  onEdit: () => void;
  onHistory: () => void;
  onRequeue: () => void;
  onDiscard: () => void;
}) {
  const { post: p, busy } = props;
  const hydrating = !!p.hydrate_requested_at;
  const loaded = !!p.hydrated_at;
  const degraded = p.edit_status === "degraded_live";
  const parked = ["parked_stale", "parked_gone", "failed"].includes(p.edit_status ?? "");

  return (
    <li
      className={cn(
        "rounded border bg-surface p-3",
        degraded ? "border-danger-border" : parked ? "border-warn-border" : "border-border",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {degraded && <AlertTriangle className="h-4 w-4 shrink-0 text-danger-fg" />}
            <span className="truncate font-medium">{p.title || "(untitled)"}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-subtle">
            <span>{p.account}</span>
            <span>#{p.post_id}</span>
            <span>posted {fmt(p.posted_ts)}</span>
            {loaded && <span>loaded {fmt(p.hydrated_at)}</span>}
            {p.has_pending_edit && (
              <span className="text-accent-soft-fg">
                pending rev {p.live_rev} → {p.desired_rev}
              </span>
            )}
          </div>

          {degraded && (
            <p className="mt-2 rounded bg-danger px-2 py-1 text-xs text-danger-fg">
              This posting is live and degraded — a reconcile changed it and could
              not finish{p.failed_step ? ` (failed at ${p.failed_step})` : ""}.
              {p.failed_message ? ` ${p.failed_message}` : ""}
            </p>
          )}
          {parked && !degraded && (
            <p className="mt-2 text-xs text-warn-fg">
              {p.edit_status === "parked_stale"
                ? "The live posting changed since you edited it, so it was not overwritten. Load it again and redo your edit."
                : p.edit_status === "parked_gone"
                  ? "Craigslist no longer lists this posting as editable."
                  : `Failed${p.failed_step ? ` at ${p.failed_step}` : ""}. ${p.failed_message ?? ""}`}
            </p>
          )}
          {p.hydrate_error && (
            <p className="mt-2 text-xs text-danger-fg">Load failed: {p.hydrate_error}</p>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap gap-1">
          <Action
            label={hydrating ? "Loading…" : loaded ? "Reload" : "Load"}
            icon={hydrating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            onClick={props.onHydrate}
            busy={busy || hydrating}
          />
          <Action label="Edit" onClick={props.onEdit} busy={busy} disabled={!loaded} />
          <Action label="History" onClick={props.onHistory} busy={busy} />
          {(parked || degraded) && (
            <Action label="Requeue" onClick={props.onRequeue} busy={busy} />
          )}
          {p.has_pending_edit && (
            <Action label="Discard" onClick={props.onDiscard} busy={busy} danger />
          )}
        </div>
      </div>
    </li>
  );
}

function Action(props: {
  label: string;
  onClick: () => void;
  busy: boolean;
  danger?: boolean;
  disabled?: boolean;
  icon?: React.ReactNode;
}) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.busy || props.disabled}
      title={props.disabled ? "Load the post from Craigslist first" : undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs",
        "hover:bg-bg disabled:opacity-40",
        props.danger && "text-danger-fg",
      )}
    >
      {props.icon}
      {props.label}
    </button>
  );
}

function EditDialog(props: {
  post: Post;
  onClose: () => void;
  onSave: (patch: Record<string, string>) => Promise<void>;
}) {
  const p = props.post;
  const [title, setTitle] = useState(p.title ?? "");
  const [body, setBody] = useState(p.body ?? "");
  const [saving, setSaving] = useState(false);

  const dirty = title !== (p.title ?? "") || body !== (p.body ?? "");

  return (
    <Modal open onOpenChange={(o) => !o && props.onClose()} title={`Edit #${p.post_id}`}>
      <div className="space-y-3">
        <p className="text-xs text-fg-muted">
          Saving queues the change. The desktop applies it when it next has a free
          browser and the edit window is open — it will not happen right now.
        </p>
        <label className="block">
          <span className="text-xs text-fg-subtle">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="mt-1 w-full rounded border border-border bg-bg px-2 py-1.5 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs text-fg-subtle">Body</span>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={16}
            className="mt-1 w-full rounded border border-border bg-bg px-2 py-1.5 font-mono text-xs"
          />
        </label>
        <div className="flex justify-end gap-2">
          <button
            onClick={props.onClose}
            className="rounded border border-border px-3 py-1.5 text-sm"
          >
            Cancel
          </button>
          <button
            disabled={!dirty || saving}
            onClick={async () => {
              setSaving(true);
              try {
                await props.onSave({ title, body });
              } finally {
                setSaving(false);
              }
            }}
            className="rounded bg-accent px-3 py-1.5 text-sm text-accent-fg disabled:opacity-40"
          >
            {saving ? "Queueing…" : "Queue change"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function HistoryDialog({ post, onClose }: { post: Post; onClose: () => void }) {
  const attempts = useQuery({
    queryKey: ["edits", post.post_id, "attempts"],
    queryFn: () => api.get<{ attempts: Attempt[] }>(`/edits/${post.post_id}/attempts`),
  });

  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={`History — #${post.post_id}`}>
      {attempts.isLoading && <p className="text-sm text-fg-muted">Loading…</p>}
      {attempts.data?.attempts.length === 0 && (
        <p className="text-sm text-fg-muted">No attempts yet.</p>
      )}
      <ul className="space-y-3">
        {(attempts.data?.attempts ?? []).map((a) => (
          <li key={a.event_id} className="rounded border border-border p-2">
            <div className="flex flex-wrap justify-between gap-2 text-sm">
              <span className="font-medium">{a.outcome}</span>
              <span className="text-fg-subtle">
                {fmt(a.ts)}
                {a.duration_seconds ? ` · ${a.duration_seconds.toFixed(1)}s` : ""}
              </span>
            </div>
            {a.error_message && (
              <p className="mt-1 text-xs text-danger-fg">
                {a.failed_step ? `${a.failed_step}: ` : ""}
                {a.error_message}
              </p>
            )}
            {a.steps.length > 0 && (
              <ol className="mt-2 space-y-0.5 font-mono text-[11px] text-fg-subtle">
                {a.steps.map((s, i) => (
                  <li key={i} className={cn(!s.ok && "text-danger-fg")}>
                    {s.ok ? "ok  " : "FAIL"} {s.name}
                    {s.duration_seconds != null ? ` ${s.duration_seconds.toFixed(1)}s` : ""}
                    {s.note ? ` — ${s.note}` : ""}
                  </li>
                ))}
              </ol>
            )}
            {a.artifact_ids.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-2">
                {a.artifact_ids.map((id) => (
                  <a
                    key={id}
                    href={`${import.meta.env.VITE_API_BASE_URL || "/api"}/artifacts/${id}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs underline text-accent-soft-fg"
                  >
                    artifact {id.slice(0, 8)}
                  </a>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </Modal>
  );
}
