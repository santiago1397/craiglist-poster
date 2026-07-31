// Review module (phase 1).
//
// One screen for everything needing attention: unreviewed drafts, drafts parked
// after a failed post, and queue health. Review is triage, not a gate — drafts
// post whether or not they have been read (decision 22), so this page's job is
// to make it easy to catch one before it goes rather than to hold it back.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { ConfirmDialog, Modal, RawModal } from "../components/Modal";
import { formatDate, formatDateTime, formatDayLabel, formatTime } from "../lib/format";

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
  not_before: string | null;
  expires_at: string | null;
  failed_step: string | null;
  failed_message: string | null;
  attempts: number;
  created_at: string;
  generated_by: string | null; // 'ai' | 'fallback' | null when hand-written
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

type CountyRef = {
  name: string;
  subarea_supported: boolean;
  cities: { city: string; zip: string }[];
};

type LocationRef = {
  counties: CountyRef[];
  phone_numbers: string[];
  license_number: string;
  service_offered: string;
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

// Craigslist accepts 24 images per posting: one thumbnail plus 23 more.
const MAX_IMAGE_SLOTS = 24;

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
  const [filter, setFilter] = useState<FilterKey>("needs_attention");
  const [editing, setEditing] = useState<Draft | null>(null);
  const [creating, setCreating] = useState(false);
  const [previewing, setPreviewing] = useState<Draft | null>(null);
  const [deleting, setDeleting] = useState<Draft | null>(null);

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

      {generation && <GenerationStatus g={generation} />}

      {health && <QueueHealth health={health} accounts={accounts} />}

      {schedule.length > 0 && <Calendar entries={schedule} />}

      <div className="flex gap-1">
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
              onDelete={() => setDeleting(d)}
            />
          ))}
        </ul>
      )}

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
  const [f, setF] = useState({
    account: props.accounts[0] ?? "",
    title: "",
    body: "",
    city: "",
    county: "",
    postal_code: "",
    geographic_area: "",
    phone_number: "",
    license_number: "",
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) =>
    setF({ ...f, [k]: e.target.value });

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

  const county = L?.counties.find((c) => c.name === f.county) ?? null;

  // Changing county invalidates the chosen city, so clear both it and the zip
  // rather than leaving a Broward city sitting under Palm Beach.
  const onCounty = (name: string) =>
    setF({ ...f, county: name, city: "", postal_code: "" });

  // Selecting a city fills its zip, still editable afterwards.
  // Picking a city seeds the free-text area box too, but leaves it editable —
  // widening it to nearby towns is the whole point of the separate field.
  const onCity = (city: string) => {
    const hit = county?.cities.find((c) => c.city === city);
    setF({
      ...f,
      city,
      postal_code: hit?.zip ?? f.postal_code,
      geographic_area: f.geographic_area.trim() ? f.geographic_area : city,
    });
  };

  const valid = f.account && f.title.trim() && f.body.trim() && f.county && f.city;

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
                body_head: f.body.split("\n\n.")[0].slice(0, 2000),
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
      <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {/* On a fresh database no account has posted yet, so the list is
                empty — fall back to typing the name rather than dead-ending. */}
            {props.accounts.length > 0 ? (
              <label className="block">
                <span className="text-xs text-fg-muted">Account</span>
                <select
                  value={f.account}
                  onChange={set("account")}
                  className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
                >
                  {props.accounts.map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <Field label="Account (e.g. craigs1)" value={f.account} onChange={set("account")} />
            )}
            {/* County first: it decides which cities are valid, and it is what
                the poster uses to pick the Craigslist subarea. */}
            <label className="block">
              <span className="text-xs text-fg-muted">County</span>
              <select
                value={f.county}
                onChange={(e) => onCounty(e.target.value)}
                className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
              >
                <option value="">Select…</option>
                {L?.counties.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                    {c.subarea_supported ? "" : "  (not routable)"}
                  </option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="text-xs text-fg-muted">
                City {county ? `(${county.cities.length})` : ""}
              </span>
              <select
                value={f.city}
                onChange={(e) => onCity(e.target.value)}
                disabled={!county}
                className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm disabled:opacity-40"
              >
                <option value="">{county ? "Select…" : "Pick a county first"}</option>
                {county?.cities.map((c) => (
                  <option key={c.city} value={c.city}>
                    {c.city}
                  </option>
                ))}
              </select>
            </label>

            <Field label="Zip" value={f.postal_code} onChange={set("postal_code")} />

            <label className="block">
              <span className="text-xs text-fg-muted">Phone</span>
              <select
                value={f.phone_number}
                onChange={set("phone_number")}
                className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
              >
                {L?.phone_numbers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>

            <Field label="License" value={f.license_number} onChange={set("license_number")} />
          </div>

          {county && !county.subarea_supported && (
            <p className="text-xs text-warn-fg border border-warn-border bg-warn/60 rounded px-2 py-1.5">
              The poster cannot map <strong>{county.name}</strong> to a Craigslist
              subarea — it will fall back to the first option on the form and file
              the ad under the wrong area. Use a different county until that is
              fixed.
            </p>
          )}
          <label className="block">
            <span className="text-xs text-fg-muted">
              City or neighborhood — Craigslist's free-text area box. Accepts
              more than one place: “Fort Lauderdale, Davie, Plantation” widens
              the searches you show up in.
            </span>
            <input
              value={f.geographic_area}
              onChange={set("geographic_area")}
              placeholder={f.city || "e.g. Davie, Plantation, Cooper City"}
              className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
            />
          </label>
          <TitleField value={f.title} onChange={(v) => setF({ ...f, title: v })} />
          <label className="block">
            <span className="text-xs text-fg-muted">
              Body — paste the full posting text, keyword tail included
            </span>
            <textarea
              value={f.body}
              onChange={set("body")}
              rows={14}
              className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm font-mono"
            />
          </label>
      </>
    </Modal>
  );
}

function Field(props: {
  label: string;
  value: string;
  onChange: (e: { target: { value: string } }) => void;
}) {
  return (
    <label className="block">
      <span className="text-xs text-fg-muted">{props.label}</span>
      <input
        value={props.value}
        onChange={props.onChange}
        className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
      />
    </label>
  );
}

// Craigslist truncates posting titles around 70 characters. Nothing warned you,
// so an over-long title silently lost its ending — usually the city or the
// call to action, which is the part doing the work.
const TITLE_LIMIT = 70;

function TitleField({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const over = value.length > TITLE_LIMIT;
  return (
    <label className="block">
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-fg-muted">Title</span>
        <span className={cn("text-xs tabular-nums", over ? "text-warn-fg" : "text-fg-subtle")}>
          {value.length}/{TITLE_LIMIT}
        </span>
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-describedby="title-limit-hint"
        className={cn(
          "w-full mt-1 bg-bg border rounded px-2 py-1.5 text-sm",
          over ? "border-warn-border" : "border-border-strong",
        )}
      />
      {/* A soft warning, not a maxLength: hard-truncating a pasted title would
          throw away characters without saying so. */}
      <span
        id="title-limit-hint"
        className={cn("text-xs mt-1 block", over ? "text-warn-fg" : "sr-only")}
      >
        {over
          ? `Craigslist shows about ${TITLE_LIMIT} characters — the last ${
              value.length - TITLE_LIMIT
            } may be cut off.`
          : `Craigslist shows about ${TITLE_LIMIT} characters.`}
      </span>
    </label>
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
        {/* The whole left side toggles the preview — clicking a draft to read
            it is the obvious gesture, and Edit is a deliberate second step. */}
        <div
          className="min-w-0 cursor-pointer flex-1"
          onClick={() => setOpen((v) => !v)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && setOpen((v) => !v)}
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
          </div>
          <p className="mt-1 font-medium">
            <span className="text-fg-subtle mr-1.5 select-none">{open ? "▾" : "▸"}</span>
            {d.title}
          </p>
          <p className="text-xs text-fg-subtle mt-0.5">
            {d.city}
            {d.postal_code ? ` ${d.postal_code}` : ""} · created {fmt(d.created_at)}
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
        </div>
        <div className="flex shrink-0 gap-1">
          {/* Preview is for things not yet published — once it is live, the
              real Craigslist page is the truth, not a mock-up. */}
          {(d.status === "queued" || d.status === "needs_attention") && (
            <Action label="Preview" onClick={props.onPreview} busy={busy} />
          )}
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

      {open && (
        <div className="mt-3 border-t border-border pt-3 space-y-3">
          <div className="flex gap-4 text-xs text-fg-subtle flex-wrap">
            <span>{d.county} / {d.city} {d.postal_code}</span>
            <span>“{d.geographic_area || d.city}” in the CL area box</span>
            <span>{d.phone_number}</span>
            {d.not_before && <span>not before {fmt(d.not_before)}</span>}
            {d.expires_at && <span>expires {fmt(d.expires_at)}</span>}
          </div>
          <DraftImages draftId={d.id} account={d.account} busy={busy} />
          <pre className="text-xs text-fg-muted whitespace-pre-wrap font-mono max-h-96 overflow-auto bg-bg/60 rounded p-2">
            {d.body}
          </pre>
        </div>
      )}
    </li>
  );
}

// Attached images, plus a picker over the approved stack. Only images this
// account may use are offered — one already claimed by another account would be
// rejected by the server anyway, so it is never shown.
function DraftImages(props: { draftId: number; account: string; busy: boolean }) {
  const [attached, setAttached] = useState<{ id: number; slot: number }[]>([]);
  const [pool, setPool] = useState<{ id: number; kind: string }[]>([]);
  const [picking, setPicking] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ images: { id: number; slot: number }[] }>(
        `/images/draft/${props.draftId}`,
      );
      setAttached(r.images);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }, [props.draftId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function openPicker() {
    setErr(null);
    try {
      const r = await api.get<{ images: { id: number; kind: string }[] }>("/images", {
        status: "approved",
        account: props.account,
        limit: 60,
      });
      setPool(r.images);
      setPicking(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function act(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }

  // Slot 1 is the Craigslist thumbnail, so new attachments land at the end
  // rather than silently displacing the cover.
  const nextSlot = Math.min(MAX_IMAGE_SLOTS, (attached.length ? Math.max(...attached.map((a) => a.slot)) : 0) + 1);
  const free = pool.filter((p) => !attached.some((a) => a.id === p.id));

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-fg-muted">
          Images ({attached.length}/{MAX_IMAGE_SLOTS}){attached.length ? " — slot 1 is the thumbnail" : ""}
        </span>
        {attached.length < MAX_IMAGE_SLOTS && (
          <button
            disabled={props.busy}
            onClick={() => void openPicker()}
            className="text-xs px-2 py-0.5 rounded border border-border-strong text-fg-muted hover:bg-surface-2 disabled:opacity-40"
          >
            + Attach image
          </button>
        )}
      </div>
      {err && <p className="text-xs text-danger-fg">{err}</p>}

      {attached.length === 0 ? (
        <p className="text-xs text-fg-subtle italic">
          No images — this post will go out text-only.
        </p>
      ) : (
        <ul className="flex gap-2 flex-wrap">
          {attached.map((a) => (
            <li key={a.id} className="relative">
              {/* There is no thumbnail route — /raw serves the full file, and
                  generated images run 300-550KB. A draft at the 24-slot limit
                  was therefore ~10MB of full-resolution downloads the moment
                  you expanded its row, on whatever connection you were on.
                  Lazy + async decode means only what you actually scroll to. */}
              <img
                src={`${IMG_BASE}/images/${a.id}/raw`}
                alt={a.slot === 1 ? "Cover image (Craigslist thumbnail)" : `Image in slot ${a.slot}`}
                loading="lazy"
                decoding="async"
                width={112}
                height={80}
                className="h-20 w-28 object-cover rounded border border-border-strong bg-surface-2"
              />
              <span className="absolute top-0.5 left-0.5 text-[10px] px-1 rounded bg-black/70 text-white">
                {a.slot === 1 ? "cover" : a.slot}
              </span>
              <button
                onClick={() => act(() => api.del(`/images/draft/${props.draftId}/attach/${a.id}`))}
                className="absolute top-0.5 right-0.5 text-[10px] px-1 rounded bg-black/70 text-danger-fg hover:bg-danger"
                title="Detach"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}

      {picking && (
        <div className="border border-border-strong rounded p-2 bg-bg/60">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-fg-muted">
              Approved and available to {props.account}
            </span>
            <button
              onClick={() => setPicking(false)}
              className="text-xs text-fg-muted hover:text-fg px-1"
            >
              close
            </button>
          </div>
          {free.length === 0 ? (
            <p className="text-xs text-fg-subtle">
              Nothing available. Generate and approve images on the Images page.
            </p>
          ) : (
            <ul className="flex gap-2 flex-wrap max-h-44 overflow-auto">
              {free.map((p) => (
                <li key={p.id}>
                  <button
                    onClick={() =>
                      act(async () => {
                        await api.post(`/images/draft/${props.draftId}/attach`, {
                          image_id: p.id,
                          slot: nextSlot,
                        });
                        setPicking(false);
                      })
                    }
                    className="block relative"
                    title={`Attach as slot ${nextSlot}`}
                  >
                    <img
                      src={`${IMG_BASE}/images/${p.id}/raw`}
                      alt={`Image ${p.id}${p.kind === "cover" ? ", cover" : ""}`}
                      loading="lazy"
                      decoding="async"
                      width={96}
                      height={64}
                      className="h-16 w-24 object-cover rounded border border-border-strong bg-surface-2 hover:border-ring"
                    />
                    {p.kind === "cover" && (
                      <span className="absolute bottom-0.5 left-0.5 text-[10px] px-1 rounded bg-black/70 text-warn-fg">
                        cover
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// A deliberately faithful mock of a Craigslist posting page, so you can judge
// the ad as a buyer sees it rather than as a database row. Rendered light-on-
// white regardless of the dashboard theme, because that is what Craigslist is —
// and because the contrast is exactly what makes the disclaimer unmissable.
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
                <img
                  src={`${IMG_BASE}/images/${images[active]?.id}/raw`}
                  alt={`${active + 1} of ${images.length}`}
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
                      <img
                        src={`${IMG_BASE}/images/${img.id}/raw`}
                        alt=""
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

function Action(props: { label: string; onClick: () => void; busy: boolean; danger?: boolean }) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.busy}
      className={cn(
        "text-xs px-2 py-1 rounded border disabled:opacity-40",
        props.danger
          ? "border-danger-border text-danger-fg hover:bg-danger"
          : "border-border-strong text-fg-muted hover:bg-surface-2",
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
  const [geo, setGeo] = useState(props.draft.geographic_area ?? props.draft.city);

  // Escape now closes the dialog (Radix), so a 14,000-character body needs a
  // guard or a stray keypress silently discards the edit.
  const dirty =
    title !== props.draft.title ||
    body !== props.draft.body ||
    geo !== (props.draft.geographic_area ?? props.draft.city);

  return (
    <Modal
      open
      onOpenChange={(o) => !o && props.onClose()}
      onRequestClose={() => !dirty || confirm("Discard your unsaved changes to this draft?")}
      title={`Edit draft #${props.draft.id}`}
      footer={
        <>
          <button
            onClick={props.onClose}
            className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            onClick={() => void props.onSave({ title, body, geographic_area: geo, reviewed: true })}
            className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover"
          >
            Save &amp; mark reviewed
          </button>
        </>
      }
    >
      <>
          <TitleField value={title} onChange={setTitle} />
          <label className="block">
            <span className="text-xs text-fg-muted">
              City or neighborhood — goes in Craigslist's free-text area box.
              Not limited to one city: “Fort Lauderdale, Davie, Plantation” or a
              neighbourhood both work, and widen the searches you appear in.
            </span>
            <input
              value={geo}
              onChange={(e) => setGeo(e.target.value)}
              placeholder={props.draft.city}
              className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs text-fg-muted">
              Body — the keyword tail is part of this text; edit the top section
            </span>
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={16}
              className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm font-mono"
            />
          </label>
      </>
    </Modal>
  );
}
