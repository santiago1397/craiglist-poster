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
  const [creating, setCreating] = useState(false);
  const [posting, setPosting] = useState<PostingState | null>(null);
  const [locations, setLocations] = useState<LocationRef | null>(null);
  const [generation, setGeneration] = useState<GenerationState | null>(null);
  const [schedule, setSchedule] = useState<ScheduleEntry[]>([]);
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
      const [list, accts, posting, locs, gen] = await Promise.all([
        api.get<{ drafts: Draft[] }>("/drafts", params),
        api.get<{ accounts: string[] }>("/accounts"),
        api.get<PostingState>("/settings/posting"),
        api.get<LocationRef>("/reference/locations"),
        api.get<GenerationState>("/settings/generation"),
      ]);
      setDrafts(list.drafts);
      setAccounts(accts.accounts);
      setPosting(posting);
      setLocations(locs);
      setGeneration(gen);
      if (accts.accounts.length) {
        const list = accts.accounts.join(",");
        const [h, s] = await Promise.all([
          api.get<Health>("/drafts/health", { accounts: list }),
          api.get<{ schedule: ScheduleEntry[] }>("/drafts/schedule", { accounts: list }),
        ]);
        setHealth(h);
        setSchedule(s.schedule);
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
        <div className="flex gap-2">
          <button
            disabled={busy}
            onClick={() => mutate(() => api.post("/drafts/generate", { force: true, limit: 10 }))}
            className="text-sm px-3 py-1 rounded bg-violet-700 hover:bg-violet-600 disabled:opacity-40"
            title="Fill the queue now instead of waiting for the background job"
          >
            {busy ? "Generating…" : "Generate now"}
          </button>
          <button
            onClick={() => setCreating(true)}
            className="text-sm px-3 py-1 rounded bg-sky-700 hover:bg-sky-600"
          >
            New draft
          </button>
          <button
            onClick={() => void load()}
            className="text-sm px-2 py-1 rounded hover:bg-slate-800 text-slate-300"
          >
            Refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      {posting && (
        <PostingSwitch
          state={posting}
          busy={busy}
          onToggle={(enabled, reason) =>
            mutate(() => api.put("/settings/posting", { enabled, reason }))
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
  const onCity = (city: string) => {
    const hit = county?.cities.find((c) => c.city === city);
    setF({ ...f, city, postal_code: hit?.zip ?? f.postal_code });
  };

  const valid = f.account && f.title.trim() && f.body.trim() && f.county && f.city;

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-4 z-10">
      <div className="bg-slate-900 border border-slate-700 rounded-lg w-full max-w-3xl max-h-[90vh] flex flex-col">
        <div className="p-3 border-b border-slate-800 flex items-center justify-between">
          <h2 className="font-medium">New draft</h2>
          <button onClick={props.onClose} className="text-slate-400 hover:text-white px-2">
            ✕
          </button>
        </div>
        <div className="p-3 space-y-3 overflow-auto">
          <div className="grid grid-cols-2 gap-3">
            {/* On a fresh database no account has posted yet, so the list is
                empty — fall back to typing the name rather than dead-ending. */}
            {props.accounts.length > 0 ? (
              <label className="block">
                <span className="text-xs text-slate-400">Account</span>
                <select
                  value={f.account}
                  onChange={set("account")}
                  className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm"
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
              <span className="text-xs text-slate-400">County</span>
              <select
                value={f.county}
                onChange={(e) => onCounty(e.target.value)}
                className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm"
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
              <span className="text-xs text-slate-400">
                City {county ? `(${county.cities.length})` : ""}
              </span>
              <select
                value={f.city}
                onChange={(e) => onCity(e.target.value)}
                disabled={!county}
                className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm disabled:opacity-40"
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
              <span className="text-xs text-slate-400">Phone</span>
              <select
                value={f.phone_number}
                onChange={set("phone_number")}
                className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm"
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
            <p className="text-xs text-amber-300 border border-amber-800/60 bg-amber-950/30 rounded px-2 py-1.5">
              The poster cannot map <strong>{county.name}</strong> to a Craigslist
              subarea — it will fall back to the first option on the form and file
              the ad under the wrong area. Use a different county until that is
              fixed.
            </p>
          )}
          <Field label="Title" value={f.title} onChange={set("title")} />
          <label className="block">
            <span className="text-xs text-slate-400">
              Body — paste the full posting text, keyword tail included
            </span>
            <textarea
              value={f.body}
              onChange={set("body")}
              rows={14}
              className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm font-mono"
            />
          </label>
        </div>
        <div className="p-3 border-t border-slate-800 flex justify-between items-center">
          <span className="text-xs text-slate-500">
            Goes to the back of {f.account || "the"} queue. Use Top to promote it.
          </span>
          <div className="flex gap-2">
            <button
              onClick={props.onClose}
              className="px-3 py-1.5 rounded text-sm text-slate-300 hover:bg-slate-800"
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
              className="px-3 py-1.5 rounded text-sm bg-sky-700 hover:bg-sky-600 disabled:opacity-40"
            >
              Create
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field(props: {
  label: string;
  value: string;
  onChange: (e: { target: { value: string } }) => void;
}) {
  return (
    <label className="block">
      <span className="text-xs text-slate-400">{props.label}</span>
      <input
        value={props.value}
        onChange={props.onChange}
        className="w-full mt-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm"
      />
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
      <section className="rounded border border-amber-600 bg-amber-950/40 p-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <p className="font-semibold text-amber-200">Posting is paused</p>
            <p className="text-xs text-amber-200/70 mt-0.5">
              Paused {fmt(state.paused_at)}
              {state.paused_reason ? ` — ${state.paused_reason}` : ""}. The queue is
              untouched; drafts resume in the same order.
            </p>
          </div>
          <button
            disabled={busy}
            onClick={() => props.onToggle(true)}
            className="px-4 py-1.5 rounded text-sm bg-emerald-700 hover:bg-emerald-600 disabled:opacity-40 shrink-0"
          >
            Resume posting
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded border border-slate-800 bg-slate-900/50 p-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-emerald-500" />
          <span className="text-sm text-slate-300">Posting is active</span>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (optional)"
            className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-xs w-48"
          />
          <button
            disabled={busy}
            onClick={() => props.onToggle(false, reason || undefined)}
            className="px-4 py-1.5 rounded text-sm bg-amber-700 hover:bg-amber-600 disabled:opacity-40"
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
        degraded ? "border-amber-800/70 bg-amber-950/20" : "border-slate-800 bg-slate-900/50",
      )}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-slate-300">Auto-generation</span>
          <span
            className={cn(
              "text-xs px-1.5 py-0.5 rounded",
              g.enabled ? "bg-emerald-900/60 text-emerald-200" : "bg-slate-800 text-slate-400",
            )}
          >
            {g.enabled ? "on" : "off"}
          </span>
          <span className="text-xs text-slate-500">{g.model}</span>
          {!g.api_key_configured && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-200">
              no API key — using workbook copy
            </span>
          )}
        </div>
        <div className="text-xs text-slate-500">
          {g.seed_ads} seed ads · {g.generated_total} AI / {g.fallback_total} fallback
          {g.last_run_at ? ` · last run ${fmt(g.last_run_at)}` : " · never run"}
        </div>
      </div>
      {g.last_error && (
        <p className="text-xs text-amber-200/80 mt-1.5">
          Last generation error: {g.last_error}
        </p>
      )}
      {g.seed_ads === 0 && (
        <p className="text-xs text-amber-200 mt-1.5">
          No seed ads loaded — there is nothing to fall back to if the model
          fails. Run <code>scripts/import_seed_ads.py</code>.
        </p>
      )}
    </section>
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

// Forecast, not a promise: pausing, a failed post or an edit all shift it. Says
// so plainly rather than implying these are committed times.
function Calendar({ entries }: { entries: ScheduleEntry[] }) {
  const [open, setOpen] = useState(false);
  const days = new Map<string, ScheduleEntry[]>();
  for (const e of entries) {
    const key = new Date(e.at).toLocaleDateString(undefined, {
      weekday: "short", month: "short", day: "numeric",
    });
    days.set(key, [...(days.get(key) ?? []), e]);
  }
  const shown = open ? [...days] : [...days].slice(0, 3);
  const last = entries[entries.length - 1];

  return (
    <section className="rounded border border-slate-800 bg-slate-900/50 p-3">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-2">
        <div>
          <span className="text-sm text-slate-300">Projected schedule</span>
          <span className="text-xs text-slate-500 ml-2">
            {entries.length} drafts · clears {new Date(last.at).toLocaleDateString(undefined, {
              month: "short", day: "numeric",
            })}
          </span>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-xs px-2 py-1 rounded border border-slate-700 text-slate-300 hover:bg-slate-800"
        >
          {open ? "Show less" : `Show all ${days.size} days`}
        </button>
      </div>
      <div className="space-y-1.5">
        {shown.map(([day, list]) => (
          <div key={day} className="flex gap-3 text-xs">
            <span className="text-slate-500 w-28 shrink-0">{day}</span>
            <div className="flex-1 space-y-0.5">
              {list.map((e) => (
                <div key={e.draft_id} className="flex gap-2">
                  <span className="text-slate-500 w-16 shrink-0">
                    {new Date(e.at).toLocaleTimeString(undefined, {
                      hour: "numeric", minute: "2-digit",
                    })}
                  </span>
                  <span className="text-slate-400 w-20 shrink-0">{e.account}</span>
                  <span className="text-slate-300 truncate">{e.title}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-slate-600 mt-2">
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
        parked ? "border-amber-800/70 bg-amber-950/20" : "border-slate-800 bg-slate-900/40",
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
            <span className="text-xs text-slate-500">#{d.id}</span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">
              {d.account}
            </span>
            {!d.reviewed && d.status === "queued" && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-sky-900/60 text-sky-200">
                unreviewed
              </span>
            )}
            {d.generated_by === "ai" && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-violet-900/60 text-violet-200">
                AI
              </span>
            )}
            {d.generated_by === "fallback" && (
              <span
                className="text-xs px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-200"
                title="The model was unavailable, so this uses the workbook copy verbatim"
              >
                workbook copy
              </span>
            )}
            {parked && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-200">
                failed at {d.failed_step ?? "unknown step"}
              </span>
            )}
          </div>
          <p className="mt-1 font-medium">
            <span className="text-slate-500 mr-1.5 select-none">{open ? "▾" : "▸"}</span>
            {d.title}
          </p>
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

      {open && (
        <div className="mt-3 border-t border-slate-800 pt-3 space-y-2">
          <div className="flex gap-4 text-xs text-slate-500 flex-wrap">
            <span>{d.county} / {d.city} {d.postal_code}</span>
            <span>{d.phone_number}</span>
            {d.not_before && <span>not before {fmt(d.not_before)}</span>}
            {d.expires_at && <span>expires {fmt(d.expires_at)}</span>}
          </div>
          {/* Images arrive in a later phase; say so rather than leaving an
              unexplained gap where a thumbnail should be. */}
          <p className="text-xs text-slate-500 italic">
            No images yet — posts currently go out text-only.
          </p>
          <pre className="text-xs text-slate-300 whitespace-pre-wrap font-mono max-h-96 overflow-auto bg-slate-950/60 rounded p-2">
            {d.body}
          </pre>
        </div>
      )}
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
