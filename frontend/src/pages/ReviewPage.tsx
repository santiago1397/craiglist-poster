// Review module (phase 1).
//
// One screen for everything needing attention: unreviewed drafts, drafts parked
// after a failed post, and queue health. Review is triage, not a gate — drafts
// post whether or not they have been read (decision 22), so this page's job is
// to make it easy to catch one before it goes rather than to hold it back.

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { ChevronRight } from "lucide-react";
import { ConfirmDialog, Modal, RawModal } from "../components/Modal";
import { formatDate, formatDateTime, formatDayLabel, formatTime } from "../lib/format";
import { PostingForm } from "../components/posting/PostingForm";
import { SlotPicker, draftTarget } from "../components/images/SlotPicker";
import {
  deriveBodyHead,
  effectiveBodyLength,
  postingDirty,
  splitBody,
  POSTING_BODY_LIMIT,
  type LocationRef,
  type PostingFormValue,
} from "../lib/posting";

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
  geographic_area: string | null;
  phone_number: string;
  license_number: string;
  not_before: string | null;
  expires_at: string | null;
  failed_step: string | null;
  failed_message: string | null;
  attempts: number;
  created_at: string;
  generated_by: string | null; // 'ai' | 'fallback' | null when hand-written
  // Set when an agent API key wrote this draft rather than a person or the
  // top-up loop. `source` carries the key's label ("agent:<label>"), which is
  // what makes "who wrote this" answerable months later.
  created_by_key_id: number | null;
  source: string | null;
  // "Post now": set while the desktop has yet to pick the request up, cleared
  // by event ingest once the attempt comes back. There is no client-side
  // mirror of this — the column is the truth and the poll below reads it.
  post_requested_at: string | null;
  post_request_error: string | null;
};

type GenerationState = {
  enabled: boolean;
  model: string;
  api_key_configured: boolean;
  seed_ads: number;
  last_run_at: string | null;
  last_source: string | null;
  last_error: string | null;
  generated_total: number;
  fallback_total: number;
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

type PostingState = {
  enabled: boolean;
  paused_at: string | null;
  paused_reason: string | null;
};

type ScheduleEntry = {
  draft_id: number;
  account: string;
  title: string;
  city: string;
  at: string;
};

const FILTERS = [
  { key: "needs_attention", label: "Needs attention" },
  { key: "unreviewed", label: "Unreviewed" },
  { key: "queued", label: "Queue" },
  { key: "all", label: "All" },
] as const;

type FilterKey = (typeof FILTERS)[number]["key"];

const IMG_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

// Every other page renders timestamps in America/New_York with an explicit "ET"
// suffix (lib/format). This page used to format in the browser's own timezone
// with no label, so the same draft showed one time here and another on the
// dashboard — and a delegate in a different timezone would be quietly misled
// about when things post. The guardrail windows are evaluated in ET server
// side, so ET is the only honest thing to show.
const fmt = formatDateTime;

export default function ReviewPage() {
  const qc = useQueryClient();

  // The tab lives in the URL so it survives a reload and can be linked. It was
  // component state, so refreshing always dumped you back on the default tab.
  const [search, setSearch] = useSearchParams();
  const urlTab = search.get("tab");
  const filter: FilterKey =
    FILTERS.some((f) => f.key === urlTab) ? (urlTab as FilterKey) : "needs_attention";
  const setFilter = (k: FilterKey) =>
    setSearch(k === "needs_attention" ? {} : { tab: k }, { replace: true });
  const [editing, setEditing] = useState<Draft | null>(null);
  const [creating, setCreating] = useState(false);
  const [previewing, setPreviewing] = useState<Draft | null>(null);
  const [deleting, setDeleting] = useState<Draft | null>(null);
  const [postingNow, setPostingNow] = useState<Draft | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);

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

  // Every mutation used to call load(), which re-requested all seven endpoints
  // — including /reference/locations, which is a constant compiled into
  // reference.py. Marking one draft reviewed cost seven round trips. Each query
  // is now cached and invalidated on its own terms.
  const draftsQ = useQuery({
    queryKey: ["drafts", params],
    queryFn: () => api.get<{ drafts: Draft[] }>("/drafts", params),
    placeholderData: (prev) => prev,
    // Poll only while a "Post now" is outstanding. The desktop picks one up
    // within ~15s and you are watching for it, so this page — which otherwise
    // never polls — goes live for exactly as long as something is in flight.
    refetchInterval: (q) =>
      q.state.data?.drafts?.some((d) => d.post_requested_at) ? 5_000 : false,
  });
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<{ accounts: string[] }>("/accounts"),
    staleTime: 5 * 60_000,
  });
  const postingQ = useQuery({
    queryKey: ["settings", "posting"],
    queryFn: () => api.get<PostingState>("/settings/posting"),
  });
  const locationsQ = useQuery({
    queryKey: ["reference", "locations"],
    queryFn: () => api.get<LocationRef>("/reference/locations"),
    staleTime: Infinity, // static reference data; it cannot change at runtime
  });
  const generationQ = useQuery({
    queryKey: ["settings", "generation"],
    queryFn: () => api.get<GenerationState>("/settings/generation"),
  });

  const accounts = accountsQ.data?.accounts ?? [];
  const acctKey = accounts.join(",");

  const healthQ = useQuery({
    queryKey: ["drafts", "health", acctKey],
    queryFn: () => api.get<Health>("/drafts/health", { accounts: acctKey }),
    enabled: accounts.length > 0,
  });
  const scheduleQ = useQuery({
    queryKey: ["drafts", "schedule", acctKey],
    queryFn: () =>
      api.get<{ schedule: ScheduleEntry[] }>("/drafts/schedule", { accounts: acctKey }),
    enabled: accounts.length > 0,
  });

  // Land on a tab that has work. "Needs attention" is empty whenever things
  // are healthy — which is most of the time — so the normal landing state used
  // to be the word "Nothing here." while 20 unreviewed drafts sat one tab over.
  // Only runs when the URL did not ask for a specific tab, and only once.
  const autoPicked = useRef(false);
  useEffect(() => {
    if (autoPicked.current || urlTab || !healthQ.data) return;
    autoPicked.current = true;
    if (healthQ.data.needs_attention > 0) return; // already the default
    if (healthQ.data.unreviewed > 0) setFilter("unreviewed");
    else setFilter("queued");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [healthQ.data, urlTab]);

  const drafts = draftsQ.data?.drafts ?? [];
  const posting = postingQ.data ?? null;
  const locations = locationsQ.data ?? null;
  const generation = generationQ.data ?? null;
  const health = healthQ.data ?? null;
  const schedule = scheduleQ.data?.schedule ?? [];

  // Anything that changes drafts also changes queue depth and the forecast.
  const refreshDrafts = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["drafts"] }),
      qc.invalidateQueries({ queryKey: ["accounts"] }),
    ]);

  const mutation = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: refreshDrafts,
  });

  const settingsMutation = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });

  const mutate = (fn: () => Promise<unknown>) => mutation.mutateAsync(fn).catch(() => {});

  const busy = mutation.isPending || settingsMutation.isPending;
  const queryError =
    draftsQ.error ?? accountsQ.error ?? postingQ.error ?? healthQ.error ?? mutation.error ??
    settingsMutation.error;
  const error = queryError
    ? queryError instanceof ApiError
      ? queryError.message
      : String(queryError)
    : null;

  return (
    <div className="p-4 space-y-4">
      {/* A div, not <header>: the app shell already provides the one banner
          landmark, and a second confuses landmark navigation. */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">Review</h1>
        <div className="flex flex-wrap gap-2">
          <button
            disabled={busy}
            onClick={() => mutate(() => api.post("/drafts/generate", { force: true, limit: 10 }))}
            className="text-sm px-3 py-1 rounded bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-40"
            title="Fill the queue now instead of waiting for the background job"
          >
            {mutation.isPending ? "Working…" : "Generate now"}
          </button>
          <button
            onClick={() => setCreating(true)}
            className="text-sm px-3 py-1 rounded bg-primary text-primary-fg hover:bg-primary-hover"
          >
            New draft
          </button>
          <button
            onClick={() => void qc.invalidateQueries()}
            className="text-sm px-2 py-1 rounded hover:bg-surface-2 text-fg-muted"
          >
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded border border-danger-border bg-danger px-3 py-2 text-sm text-danger-fg"
        >
          {error}
        </div>
      )}

      {/* The kill switch stays always visible — a system that has silently
          stopped posting is the expensive failure here. Everything else that
          used to stack above the list is behind a disclosure: four sections ran
          ~640px before the first draft, which on a phone is several screens of
          scrolling to reach the thing you came for. */}
      {posting && (
        <PostingSwitch
          state={posting}
          busy={busy}
          // settingsMutation, not mutate: pausing has to invalidate the
          // settings queries so both this switch and the header status pill
          // reflect it immediately.
          onToggle={(enabled, reason) =>
            settingsMutation
              .mutateAsync(() => api.put("/settings/posting", { enabled, reason }))
              .catch(() => {})
          }
        />
      )}

      {(health || generation || schedule.length > 0) && (
        <details className="rounded border border-border bg-surface/50 group" open={detailsOpen}>
          <summary
            onClick={(e) => {
              e.preventDefault();
              setDetailsOpen((v) => !v);
            }}
            className="cursor-pointer list-none p-3 flex items-center gap-2 text-sm"
          >
            <ChevronRight
              size={16}
              aria-hidden="true"
              className={cn("shrink-0 transition-transform", detailsOpen && "rotate-90")}
            />
            <span className="text-fg-muted">Queue health and schedule</span>
            <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle">
              {health && (
                <span>
                  {Object.values(health.accounts).reduce((a, x) => a + x.queue_depth, 0)} queued
                </span>
              )}
              {schedule.length > 0 && <span>clears {formatDate(schedule[schedule.length - 1].at)}</span>}
              {generation && !generation.api_key_configured && (
                <span className="text-warn-fg">workbook copy</span>
              )}
              {health && health.global_blocks.length > 0 && (
                <span className="text-warn-fg">{health.global_blocks[0]}</span>
              )}
            </span>
          </summary>
          <div className="p-3 pt-0 space-y-3">
            {generation && <GenerationStatus g={generation} />}
            {health && <QueueHealth health={health} accounts={accounts} />}
            {schedule.length > 0 && <Calendar entries={schedule} />}
          </div>
        </details>
      )}

      <div className="flex gap-1 flex-wrap">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={cn(
              "px-3 py-1.5 rounded text-sm",
              filter === f.key ? "bg-surface-2 text-fg" : "text-fg-muted hover:bg-surface-2/60",
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

      {draftsQ.isLoading ? (
        <ul className="space-y-2" aria-hidden="true">
          {Array.from({ length: 5 }).map((_, i) => (
            <li key={i} className="rounded border border-border bg-surface/40 p-3 space-y-2">
              <div className="h-3 w-40 bg-surface-2 rounded animate-pulse" />
              <div className="h-4 w-2/3 bg-surface-2 rounded animate-pulse" />
              <div className="h-3 w-1/3 bg-surface-2 rounded animate-pulse" />
            </li>
          ))}
        </ul>
      ) : drafts.length === 0 ? (
        <p className="text-fg-subtle text-sm py-8 text-center">
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
              onPreview={() => setPreviewing(d)}
              onReview={() => mutate(() => api.patch(`/drafts/${d.id}`, { reviewed: !d.reviewed }))}
              onTop={() => mutate(() => api.post(`/drafts/${d.id}/reorder`, { after_id: null }))}
              onRequeue={() => mutate(() => api.post(`/drafts/${d.id}/requeue`))}
              onPostNow={() => setPostingNow(d)}
              onCancelPostNow={() => mutate(() => api.del(`/drafts/${d.id}/post-now`))}
              onDelete={() => setDeleting(d)}
            />
          ))}
        </ul>
      )}

      {/* Publishing is outward-facing and irreversible — it puts a real ad on a
          real account and permanently burns the images it uses. It deserves at
          least the friction Delete gets. */}
      <ConfirmDialog
        open={postingNow !== null}
        onOpenChange={(o) => !o && setPostingNow(null)}
        title={`Post draft #${postingNow?.id} now?`}
        body={
          <>
            <span className="block font-medium text-fg">{postingNow?.title}</span>
            <span className="block mt-1">
              {postingNow?.account} · {postingNow?.city}. This publishes a live
              ad on Craigslist and permanently retires the images it uses.
            </span>
            <span className="block mt-2 text-fg-subtle">
              The guardrails still apply — if this account cannot post right
              now, you will be told why and nothing will happen. It counts
              against today's cap like any scheduled post.
            </span>
          </>
        }
        busy={busy}
        onConfirm={() => {
          const id = postingNow?.id;
          setPostingNow(null);
          if (id !== undefined) void mutate(() => api.post(`/drafts/${id}/post-now`));
        }}
      />

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete draft #${deleting?.id}?`}
        body={
          <>
            <span className="block font-medium text-fg">{deleting?.title}</span>
            <span className="block mt-1">
              {deleting?.account} · {deleting?.city}. This cannot be undone.
            </span>
          </>
        }
        busy={busy}
        onConfirm={() => {
          const id = deleting?.id;
          setDeleting(null);
          if (id !== undefined) void mutate(() => api.del(`/drafts/${id}`));
        }}
      />

      {editing && (
        <EditDialog
          draft={editing}
          accounts={accounts}
          locations={locations}
          onClose={() => setEditing(null)}
          onSave={async (patch) => {
            await mutate(() => api.patch(`/drafts/${editing.id}`, patch));
            setEditing(null);
          }}
        />
      )}

      {previewing && (
        <PreviewDialog draft={previewing} onClose={() => setPreviewing(null)} />
      )}

      {creating && (
        <CreateDialog
          accounts={accounts}
          locations={locations}
          onClose={() => setCreating(false)}
          onCreate={async (payload) => {
            await mutate(() => api.post("/drafts", payload));
            setCreating(false);
          }}
        />
      )}
    </div>
  );
}

// Phase 1 has no generator yet, so drafts are written by hand here. Phase 2
// replaces this with MiniMax-drafted copy from a prompt, still landing in the
// same queue and still editable in the same dialog.
function CreateDialog(props: {
  accounts: string[];
  locations: LocationRef | null;
  onClose: () => void;
  onCreate: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const L = props.locations;
  const [f, setF] = useState<PostingFormValue>({
    account: props.accounts[0] ?? "",
    title: "",
    body: "",
    // Nothing has split this copy, so the body editor stays a single box.
    body_head: null,
    city: "",
    county: "",
    postal_code: "",
    geographic_area: "",
    phone_number: "",
    license_number: "",
  });

  // Prefill the constants once reference data lands — license and phone are
  // the same on every ad, so making you retype them only invites typos.
  useEffect(() => {
    if (!L) return;
    setF((prev) => ({
      ...prev,
      license_number: prev.license_number || L.license_number,
      phone_number: prev.phone_number || L.phone_numbers[0],
    }));
  }, [L]);

  const valid =
    f.account &&
    f.title.trim() &&
    f.body.trim() &&
    f.county &&
    f.city &&
    effectiveBodyLength(f.body) <= POSTING_BODY_LIMIT;

  // Anything typed is unsaved work; Escape and backdrop clicks must not bin it.
  const dirty = Boolean(f.title.trim() || f.body.trim() || f.city || f.county);

  return (
    <Modal
      open
      onOpenChange={(o) => !o && props.onClose()}
      onRequestClose={() =>
        !dirty || confirm("Discard this draft? Everything you typed will be lost.")
      }
      title="New draft"
      footer={
        <>
          <span className="text-xs text-fg-subtle mr-auto">
            Goes to the back of {f.account || "the"} queue. Use Top to promote it.
          </span>
          <button
            onClick={props.onClose}
            className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            disabled={!valid}
            onClick={() =>
              void props.onCreate({
                ...f,
                service_offered: L?.service_offered ?? "",
                // body_head drives the advisory similarity score; without a
                // generator to split head from tail, the body doubles as it.
                body_head: deriveBodyHead(f.body),
                source: "manual",
              })
            }
            className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
          >
            Create
          </button>
        </>
      }
    >
      <PostingForm
        value={f}
        onChange={setF}
        accounts={props.accounts}
        locations={L}
        caps={{
          accountEditable: true,
          showCounty: true,
          showGeographicArea: true,
          cityMode: "select",
        }}
      />
    </Modal>
  );
}

// The kill switch. Deliberately the loudest thing on the page when paused —
// a system that has silently stopped posting is the expensive failure here.
function PostingSwitch(props: {
  state: PostingState;
  busy: boolean;
  onToggle: (enabled: boolean, reason?: string) => void;
}) {
  const { state, busy } = props;
  const [reason, setReason] = useState("");

  if (!state.enabled) {
    return (
      <section className="rounded border border-warn-border bg-warn p-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <p className="font-semibold text-warn-fg">Posting is paused</p>
            <p className="text-xs text-warn-fg/70 mt-0.5">
              Paused {fmt(state.paused_at)}
              {state.paused_reason ? ` — ${state.paused_reason}` : ""}. The queue is
              untouched; drafts resume in the same order.
            </p>
          </div>
          <button
            disabled={busy}
            onClick={() => props.onToggle(true)}
            className="px-4 py-1.5 rounded text-sm bg-ok-solid hover:bg-ok-solid/90 disabled:opacity-40 shrink-0"
          >
            Resume posting
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded border border-border bg-surface/50 p-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-ok-solid" />
          <span className="text-sm text-fg-muted">Posting is active</span>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (optional)"
            className="bg-bg border border-border-strong rounded px-2 py-1 text-xs w-48"
          />
          <button
            disabled={busy}
            onClick={() => props.onToggle(false, reason || undefined)}
            className="px-4 py-1.5 rounded text-sm bg-warn-solid hover:bg-warn-solid/90 disabled:opacity-40"
          >
            Stop posting
          </button>
        </div>
      </div>
    </section>
  );
}

// Surfaces the one thing that goes wrong quietly: generation silently serving
// workbook copy for weeks because the model key expired.
function GenerationStatus({ g }: { g: GenerationState }) {
  const degraded = !g.api_key_configured || g.last_source === "fallback";
  return (
    <section
      className={cn(
        "rounded border p-3 text-sm",
        degraded ? "border-warn-border bg-warn/50" : "border-border bg-surface/50",
      )}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-fg-muted">Auto-generation</span>
          <span
            className={cn(
              "text-xs px-1.5 py-0.5 rounded",
              g.enabled ? "bg-ok text-ok-fg" : "bg-surface-2 text-fg-muted",
            )}
          >
            {g.enabled ? "on" : "off"}
          </span>
          <span className="text-xs text-fg-subtle">{g.model}</span>
          {!g.api_key_configured && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-warn text-warn-fg">
              no API key — using workbook copy
            </span>
          )}
        </div>
        <div className="text-xs text-fg-subtle">
          {g.seed_ads} seed ads · {g.generated_total} AI / {g.fallback_total} fallback
          {g.last_run_at ? ` · last run ${fmt(g.last_run_at)}` : " · never run"}
        </div>
      </div>
      {g.last_error && (
        <p className="text-xs text-warn-fg/80 mt-1.5">
          Last generation error: {g.last_error}
        </p>
      )}
      {g.seed_ads === 0 && (
        <p className="text-xs text-warn-fg mt-1.5">
          No seed ads loaded — there is nothing to fall back to if the model
          fails. Run <code>scripts/import_seed_ads.py</code>.
        </p>
      )}
    </section>
  );
}

function QueueHealth({ health, accounts }: { health: Health; accounts: string[] }) {
  return (
    <section className="rounded border border-border bg-surface/50 p-3 space-y-2">
      {health.global_blocks.length > 0 && (
        <p className="text-sm text-warn-fg">
          Nothing can post right now: {health.global_blocks.join("; ")}
        </p>
      )}
      <div className="grid gap-2 sm:grid-cols-3">
        {accounts.map((name) => {
          const a = health.accounts[name];
          if (!a) return null;
          return (
            <div key={name} className="rounded bg-surface border border-border p-2">
              <div className="flex items-center justify-between">
                <span className="font-medium text-sm">{name}</span>
                <span
                  className={cn(
                    "text-xs px-1.5 py-0.5 rounded",
                    a.queue_depth === 0
                      ? "bg-danger text-danger-fg"
                      : "bg-surface-2 text-fg-muted",
                  )}
                >
                  {a.queue_depth} queued
                </span>
              </div>
              <p className="text-xs text-fg-subtle mt-1">last post {fmt(a.last_post_at)}</p>
              {!a.eligible && (
                <p className="text-xs text-fg-muted mt-1">{a.reasons.join("; ")}</p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

// Forecast, not a promise: pausing, a failed post or an edit all shift it. Says
// so plainly rather than implying these are committed times.
function Calendar({ entries }: { entries: ScheduleEntry[] }) {
  const [open, setOpen] = useState(false);
  const days = new Map<string, ScheduleEntry[]>();
  for (const e of entries) {
    // Group by ET day, not the viewer's — a 5pm ET fire is the next calendar
    // day west of UTC, which would split one posting day across two rows.
    const key = formatDayLabel(e.at);
    days.set(key, [...(days.get(key) ?? []), e]);
  }
  const shown = open ? [...days] : [...days].slice(0, 3);
  const last = entries[entries.length - 1];

  return (
    <section className="rounded border border-border bg-surface/50 p-3">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-2">
        <div>
          <span className="text-sm text-fg-muted">Projected schedule</span>
          <span className="text-xs text-fg-subtle ml-2">
            {entries.length} drafts · clears {formatDate(last.at)}
          </span>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-surface-2"
        >
          {open ? "Show less" : `Show all ${days.size} days`}
        </button>
      </div>
      {/* min-w-0 on every flex child holding truncating text: a flex item
          defaults to min-width:auto, which let a long title push the row past
          the viewport instead of ellipsing. That was the last source of
          horizontal page scroll on a phone. */}
      <div className="space-y-1.5">
        {shown.map(([day, list]) => (
          <div key={day} className="flex gap-2 sm:gap-3 text-xs min-w-0">
            <span className="text-fg-subtle w-20 sm:w-28 shrink-0">{day}</span>
            <div className="flex-1 min-w-0 space-y-0.5">
              {list.map((e) => (
                <div key={e.draft_id} className="flex gap-2 min-w-0">
                  <span className="text-fg-subtle w-16 sm:w-20 shrink-0">
                    {formatTime(e.at)}
                  </span>
                  <span className="text-fg-muted w-16 sm:w-20 shrink-0">{e.account}</span>
                  <span className="text-fg-muted truncate min-w-0">{e.title}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-fg-subtle mt-2">
        Estimated from the 9am / 1pm / 5pm task fires and the current caps. A
        pause, a failed post or a reorder will shift it.
      </p>
    </section>
  );
}

function DraftRow(props: {
  draft: Draft;
  busy: boolean;
  onEdit: () => void;
  onPreview: () => void;
  onReview: () => void;
  onTop: () => void;
  onRequeue: () => void;
  onPostNow: () => void;
  onCancelPostNow: () => void;
  onDelete: () => void;
}) {
  const { draft: d, busy } = props;
  const parked = d.status === "needs_attention";
  const [open, setOpen] = useState(false);
  return (
    <li
      className={cn(
        "rounded border p-3",
        parked ? "border-warn-border bg-warn/50" : "border-border bg-surface/40",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        {/* The whole left side toggles the row. A real <button> rather than a
            div with role="button": it gets Enter/Space, focus and the correct
            expanded state for free, and aria-expanded tells a screen reader
            what the click does. */}
        <button
          type="button"
          className="min-w-0 flex-1 text-left"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-fg-subtle">#{d.id}</span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted">
              {d.account}
            </span>
            {!d.reviewed && d.status === "queued" && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-info text-info-fg">
                unreviewed
              </span>
            )}
            {d.generated_by === "ai" && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-accent-soft text-accent-soft-fg">
                AI
              </span>
            )}
            {/* Written through an agent API key. Distinct from the "AI" badge,
                which means the top-up loop drafted the copy: this one says an
                outside assistant composed it and nobody has read it yet. */}
            {d.created_by_key_id !== null && (
              <span
                className="text-xs px-1.5 py-0.5 rounded bg-accent-soft text-accent-soft-fg"
                title={`Composed through an agent API key (${
                  d.source || "unlabelled"
                }). It cannot publish until you mark it reviewed.`}
              >
                agent-written
              </span>
            )}
            {d.generated_by === "fallback" && (
              <span
                className="text-xs px-1.5 py-0.5 rounded bg-warn text-warn-fg"
                title="The model was unavailable, so this uses the workbook copy verbatim"
              >
                workbook copy
              </span>
            )}
            {parked && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-warn text-warn-fg">
                failed at {d.failed_step ?? "unknown step"}
              </span>
            )}
            {d.post_requested_at && (
              <span
                className="text-xs px-1.5 py-0.5 rounded bg-accent-soft text-accent-soft-fg"
                title="The desktop picks this up within about 15 seconds, then opens Chrome and posts it"
              >
                posting requested
              </span>
            )}
          </div>
          {/* An expired or failed request explains itself here. Every other
              trace of it lives in a log file on the posting machine, so without
              this line "I pressed Post now and nothing happened" has no answer
              from the dashboard. */}
          {d.post_request_error && (
            <p className="mt-1 text-xs text-warn-fg">{d.post_request_error}</p>
          )}
          {/* The ad's own title leads, with the location after it in brackets.
              Leading with the city was a workaround for generated titles being
              near-identical — but it meant the row never showed you the thing
              that actually publishes. The location still has to be here:
              without it, drafts that share a title are indistinguishable. */}
          <p className="mt-1 font-medium flex items-baseline gap-1.5">
            <ChevronRight
              size={14}
              aria-hidden="true"
              className={cn(
                "shrink-0 self-center text-fg-subtle transition-transform",
                open && "rotate-90",
              )}
            />
            <span className="line-clamp-2">
              {d.title || "(no title)"}
              <span className="font-normal text-fg-muted">
                {" "}
                ({[d.city || "no city", d.postal_code].filter(Boolean).join(" ")})
              </span>
            </span>
          </p>
          <p className="text-xs text-fg-subtle mt-0.5">
            created {fmt(d.created_at)}
            {d.attempts > 1 ? ` · ${d.attempts} attempts` : ""}
          </p>
          {parked && d.failed_message && (
            <p className="text-xs text-warn-fg/80 mt-1 line-clamp-2">{d.failed_message}</p>
          )}
          {parked && (
            <p className="text-xs text-fg-muted mt-1">
              Images were already sent to Craigslist on this attempt. Check the listing did not
              publish before requeueing.
            </p>
          )}
        </button>
        {/* Six buttons in a row fight the title for space on a phone, so below
            sm they move into the expansion as full-width targets. */}
        <div className="hidden sm:flex shrink-0 gap-1">
          <Actions {...props} draft={d} busy={busy} />
        </div>
      </div>

      {open && (
        <div className="mt-3 border-t border-border pt-3 space-y-3">
          <div className="sm:hidden grid grid-cols-2 gap-1">
            <Actions {...props} draft={d} busy={busy} wide />
          </div>
          <div className="flex gap-x-4 gap-y-1 text-xs text-fg-subtle flex-wrap">
            <span>{d.county} / {d.city} {d.postal_code}</span>
            <span>“{d.geographic_area || d.city}” in the CL area box</span>
            <span>{d.phone_number}</span>
            {d.not_before && <span>not before {fmt(d.not_before)}</span>}
            {d.expires_at && <span>expires {fmt(d.expires_at)}</span>}
          </div>
          <SlotPicker target={draftTarget(d.id, d.account)} busy={busy} />
          <DraftBody body={d.body} head={d.body_head} />
        </div>
      )}
    </li>
  );
}

// Attached images, plus pickers over the two stacks. Only images this account
// may use are offered — one already claimed by another account would be
// rejected by the server anyway, so it is never shown.
//
// The cover is a separate act from the photos, because they are separate
// decisions: slot 1 is the Craigslist thumbnail and is chosen by hand, while
// slots 2-24 are bulk and fill with one press.
function PreviewDialog(props: { draft: Draft; onClose: () => void }) {
  const d = props.draft;
  const [images, setImages] = useState<{ id: number; slot: number }[]>([]);
  const [active, setActive] = useState(0);

  useEffect(() => {
    void (async () => {
      try {
        const r = await api.get<{ images: { id: number; slot: number }[] }>(
          `/images/draft/${d.id}`,
        );
        setImages(r.images);
      } catch {
        setImages([]);
      }
    })();
  }, [d.id]);

  const area = d.geographic_area || d.city;
  const county = d.county ? `${d.county.toLowerCase()} county` : "";

  return (
    <RawModal
      open
      onOpenChange={(o) => !o && props.onClose()}
      label={`Craigslist preview of draft ${d.id}`}
    >
      <div className="bg-white text-black w-full max-w-4xl my-2 sm:my-4 rounded shadow-2xl">
        {/* Unmissable, and it stays put while you scroll the ad. */}
        <div className="sticky top-0 z-10 bg-amber-400 text-black px-3 sm:px-4 py-2.5 flex items-start justify-between gap-3 rounded-t">
          <div className="min-w-0">
            <strong className="text-sm sm:text-base">PREVIEW ONLY — NOT PUBLISHED</strong>
            <p className="text-xs mt-0.5">
              This is a mock-up of how draft #{d.id} would look on Craigslist. It
              is not live, has no real post ID, and nobody else can see it.
            </p>
          </div>
          <button
            onClick={props.onClose}
            className="shrink-0 px-3 py-1 rounded bg-black/80 text-white text-sm hover:bg-black"
          >
            Close
          </button>
        </div>

        <div className="p-3 sm:p-4 font-sans text-[13px] leading-snug">
          <div className="text-[11px] text-blue-700 mb-3">
            <span className="text-slate-600">south florida</span>
            {county && <> &gt; <span className="text-slate-600">{county}</span></>}
            {" > "}services{" > "}skilled trade services
          </div>

          <h2 className="text-[19px] font-bold mb-1">
            {d.title}
            {area && <span className="font-normal text-slate-700"> ({area})</span>}
          </h2>
          <p className="text-[11px] text-slate-500 mb-3">
            compensation: contact for estimate · employment type: contract
          </p>

          {images.length > 0 ? (
            <div className="mb-4">
              <div className="bg-slate-100 flex items-center justify-center">
                {/* Capped at 420px tall — roughly 560px wide at 4:3 — so 1024
                    covers it at 2x. The original averages 772KB and would be
                    downscaled by the browser anyway. */}
                <img
                  src={`${IMG_BASE}/images/${images[active]?.id}/thumb?w=1024`}
                  alt={`${active + 1} of ${images.length}`}
                  loading="lazy"
                  decoding="async"
                  className="max-h-[420px] w-auto object-contain"
                />
              </div>
              <p className="text-[11px] text-slate-600 mt-1">
                image {active + 1} of {images.length}
              </p>
              {images.length > 1 && (
                <div className="flex gap-1 mt-1 flex-wrap">
                  {images.map((img, i) => (
                    <button key={img.id} onClick={() => setActive(i)}>
                      {/* Filmstrip only — the main image above stays /raw,
                          since this modal is where you check what publishes. */}
                      <img
                        src={`${IMG_BASE}/images/${img.id}/thumb`}
                        alt=""
                        loading="lazy"
                        decoding="async"
                        className={cn(
                          "h-12 w-16 object-cover border-2",
                          i === active ? "border-blue-600" : "border-transparent",
                        )}
                      />
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <p className="mb-4 text-[12px] text-slate-500 italic border border-dashed border-slate-300 p-3">
              No images attached — this ad would publish as text only, with no
              thumbnail in search results.
            </p>
          )}

          {d.phone_number && (
            <p className="mb-3">
              <span className="inline-block border border-slate-400 rounded px-2 py-1 text-[12px] bg-slate-50">
                ☎ {d.phone_number}
              </span>
            </p>
          )}

          {/* Craigslist preserves the body verbatim, dot padding and keyword
              wall included. Showing it honestly is the point — it is what a
              buyer scrolls past. */}
          <div className="whitespace-pre-wrap break-words border-t border-slate-200 pt-3">
            {d.body}
          </div>

          <div className="mt-5 pt-3 border-t border-slate-200 text-[11px] text-slate-600 space-y-0.5">
            <p>post id: <span className="text-slate-400">(assigned when it publishes)</span></p>
            <p>posted: <span className="text-slate-400">not yet posted</span></p>
            <p className="text-slate-400">
              ♦ this is a preview generated by your dashboard, not a Craigslist page
            </p>
          </div>
        </div>
      </div>
    </RawModal>
  );
}

/** The row's action set, rendered inline on desktop and stacked on mobile. */
function Actions(props: {
  draft: Draft;
  busy: boolean;
  wide?: boolean;
  onEdit: () => void;
  onPreview: () => void;
  onReview: () => void;
  onTop: () => void;
  onRequeue: () => void;
  onPostNow: () => void;
  onCancelPostNow: () => void;
  onDelete: () => void;
}) {
  const { draft: d, busy, wide } = props;
  const parked = d.status === "needs_attention";
  // No client state: the DB column is the truth and the page polls it while
  // anything is outstanding, so this flips back on its own when ingest clears
  // the flag. Same approach as the Edits page's hydrate button.
  const requested = !!d.post_requested_at;
  return (
    <>
      {/* Preview is for things not yet published — once it is live, the real
          Craigslist page is the truth, not a mock-up. */}
      {(d.status === "queued" || parked) && (
        <Action label="Preview" onClick={props.onPreview} busy={busy} wide={wide} />
      )}
      <Action label="Edit" onClick={props.onEdit} busy={busy} wide={wide} />
      {d.status === "queued" && (
        <>
          {requested ? (
            <Action
              label="Cancel post"
              onClick={props.onCancelPostNow}
              busy={busy}
              wide={wide}
            />
          ) : (
            <Action label="Post now" onClick={props.onPostNow} busy={busy} wide={wide} />
          )}
          <Action label="Top" onClick={props.onTop} busy={busy} wide={wide} />
          <Action
            label={d.reviewed ? "Unmark" : "Reviewed"}
            onClick={props.onReview}
            busy={busy}
            wide={wide}
          />
        </>
      )}
      {parked && <Action label="Requeue" onClick={props.onRequeue} busy={busy} wide={wide} />}
      <Action label="Delete" onClick={props.onDelete} busy={busy} danger wide={wide} />
    </>
  );
}

/**
 * The body is ~14,200 characters, of which ~13,000 are the shared keyword tail.
 * Dumping all of it into a scroll box is what made the list feel unnavigable.
 * Splitting is exact when the stored head is a prefix of the body — which is
 * how the generator assembles it — and falls back to showing everything when it
 * is not, rather than guessing and hiding real copy.
 */
function DraftBody({ body, head }: { body: string; head: string | null }) {
  const [showTail, setShowTail] = useState(false);
  const { splittable, head: copy, tail } = splitBody(body, head);

  return (
    <div className="space-y-2">
      <pre className="text-xs text-fg-muted whitespace-pre-wrap font-mono max-h-96 overflow-auto bg-bg/60 rounded p-2">
        {copy}
      </pre>
      {splittable && (
        <>
          <button
            onClick={() => setShowTail((v) => !v)}
            aria-expanded={showTail}
            className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-surface-2"
          >
            {showTail ? "Hide" : "Show"} keyword tail ({tail.length.toLocaleString()} characters)
          </button>
          {showTail && (
            <pre className="text-xs text-fg-subtle whitespace-pre-wrap font-mono max-h-72 overflow-auto bg-bg/60 rounded p-2">
              {tail}
            </pre>
          )}
        </>
      )}
    </div>
  );
}

function Action(props: {
  label: string;
  onClick: () => void;
  busy: boolean;
  danger?: boolean;
  wide?: boolean;
}) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.busy}
      className={cn(
        "text-xs rounded border disabled:opacity-40",
        // 40px tall on mobile so it is a comfortable touch target.
        props.wide ? "px-3 py-2.5 w-full" : "px-2 py-1",
        props.danger
          ? "border-danger-border text-danger-fg hover:bg-danger"
          : "border-border-strong text-fg-muted hover:bg-surface-2",
      )}
    >
      {props.label}
    </button>
  );
}

// Editing shows the same fields as creating. It used to expose only title,
// area and body — so a draft's account, county, city, zip, phone and licence
// were visible nowhere you could change them, and "where is this going to
// post?" had no answer short of reading the collapsed summary row. The PATCH
// endpoint accepted all of them the whole time; only the form was missing.
function EditDialog(props: {
  draft: Draft;
  accounts: string[];
  locations: LocationRef | null;
  onClose: () => void;
  onSave: (patch: Record<string, unknown>) => Promise<void>;
}) {
  const d = props.draft;
  const initial: PostingFormValue = {
    account: d.account,
    county: d.county ?? "",
    city: d.city ?? "",
    postal_code: d.postal_code ?? "",
    phone_number: d.phone_number ?? "",
    license_number: d.license_number ?? "",
    title: d.title,
    body: d.body,
    body_head: d.body_head,
    geographic_area: d.geographic_area ?? d.city,
  };
  const [f, setF] = useState<PostingFormValue>(initial);

  // Escape closes the dialog (Radix), so a 14,000-character body needs a guard
  // or a stray keypress silently discards the edit.
  const dirty = postingDirty(f, initial);
  const overLimit = effectiveBodyLength(f.body) > POSTING_BODY_LIMIT;
  const split = splitBody(f.body, f.body_head);

  return (
    <Modal
      open
      onOpenChange={(o) => !o && props.onClose()}
      onRequestClose={() => !dirty || confirm("Discard your unsaved changes to this draft?")}
      title={`Edit draft #${d.id}`}
      footer={
        <>
          <button
            onClick={props.onClose}
            className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            // The server rejects an over-length body with a 422, so saving would
            // only bounce. Blocking here says so before the round trip.
            disabled={overLimit}
            title={
              overLimit
                ? "The body is over Craigslist's limit — shorten it before saving"
                : undefined
            }
            onClick={() =>
              // body_head goes with it. This used to send only `body`, leaving
              // body_head frozen at whatever the generator wrote — and
              // similarity_report scores duplicate detection against body_head
              // alone, so every edit silently decoupled the score from the copy
              // that would actually publish.
              void props.onSave({
                account: f.account,
                county: f.county,
                city: f.city,
                postal_code: f.postal_code,
                phone_number: f.phone_number,
                license_number: f.license_number,
                title: f.title,
                body: f.body,
                body_head: split.splittable ? split.head : deriveBodyHead(f.body),
                geographic_area: f.geographic_area,
                reviewed: true,
              })
            }
            className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Save &amp; mark reviewed
          </button>
        </>
      }
    >
      <PostingForm
        value={f}
        onChange={setF}
        accounts={props.accounts}
        locations={props.locations}
        caps={{
          accountEditable: true,
          showCounty: true,
          showGeographicArea: true,
          cityMode: "select",
        }}
      />
    </Modal>
  );
}
