// Settings.
//
// These endpoints have existed since the queue was built but had no UI, so the
// only way to change a cap was SQL on the VPS — even though the README tells
// you to "tune the first five in the dashboard under Settings -> Guardrails".
//
// The important detail is the ceilings. The desktop clamps whatever the server
// sends to constants compiled into craigslist_auto/config.py and reports a
// flow_error when it has to. A form that accepted "30 posts a day" and looked
// like it worked, while the machine quietly did 5, would be worse than no form
// at all — so every ceiling is stated inline and enforced by the input.

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Pause, Play } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { formatDateTime } from "../lib/format";

type Guardrails = {
  min_hours_between_posts_same_account: number;
  max_posts_per_day_total: number;
  max_posts_per_account_per_week: number;
  post_window_start_hour: number;
  post_window_end_hour: number;
  post_weekdays_only: boolean;
  queue_depth_floor: number;
  queue_depth_target: number;
};

type Generation = {
  enabled: boolean;
  model: string;
  api_base: string;
  temperature: number;
  photos_min: number;
  photos_max: number;
  imageless_rate: number;
  image_topup_enabled: boolean;
  image_stack_floor: number;
  image_stack_target: number;
  image_topup_batch: number;
  api_key_configured: boolean;
  seed_ads: number;
  generated_total: number;
  fallback_total: number;
  last_error: string | null;
};

// Mirrors craigslist_auto/config.py. Kept in sync by hand — these are compiled
// into the desktop and changing one is a deliberate code change plus redeploy,
// so they move roughly never.
const LIMITS = {
  maxPostsPerDay: 5,
  maxPostsPerWeek: 10,
  minCooldownHours: 18,
  earliestHour: 6,
  latestHour: 22,
};

export default function SettingsPage() {
  const qc = useQueryClient();
  const [saved, setSaved] = useState<string | null>(null);

  const guardrailsQ = useQuery({
    queryKey: ["settings", "guardrails"],
    queryFn: () => api.get<Guardrails>("/settings/guardrails"),
  });
  const generationQ = useQuery({
    queryKey: ["settings", "generation"],
    queryFn: () => api.get<Generation>("/settings/generation"),
  });

  const saveGuardrails = useMutation({
    mutationFn: (patch: Partial<Guardrails>) => api.put("/settings/guardrails", patch),
    onSuccess: () => {
      setSaved("Guardrails saved. Machines pick this up within two minutes.");
      void qc.invalidateQueries({ queryKey: ["settings"] });
      void qc.invalidateQueries({ queryKey: ["drafts"] });
    },
  });

  const saveGeneration = useMutation({
    mutationFn: (patch: Partial<Generation>) => api.put("/settings/generation", patch),
    onSuccess: () => {
      setSaved("Generation settings saved.");
      void qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const err = saveGuardrails.error ?? saveGeneration.error ?? guardrailsQ.error;
  const errorText = err ? (err instanceof ApiError ? err.message : String(err)) : null;

  return (
    <div className="p-4 sm:p-6 space-y-6 max-w-4xl">
      <h1 className="text-lg font-semibold">Settings</h1>

      {errorText && (
        <div
          role="alert"
          className="rounded border border-danger-border bg-danger px-3 py-2 text-sm text-danger-fg"
        >
          {errorText}
        </div>
      )}
      {saved && !errorText && (
        <div className="rounded border border-ok-border bg-ok px-3 py-2 text-sm text-ok-fg flex items-center gap-2">
          <Check size={16} aria-hidden="true" />
          {saved}
        </div>
      )}

      {guardrailsQ.data && generationQ.data && (
        <QueueSizeSection
          guardrails={guardrailsQ.data}
          generation={generationQ.data}
          busy={saveGuardrails.isPending || saveGeneration.isPending}
          onSaveDepth={(patch) => {
            setSaved(null);
            saveGuardrails.mutate(patch);
          }}
          onToggleGeneration={(enabled) => {
            setSaved(null);
            saveGeneration.mutate({ enabled });
          }}
        />
      )}

      {guardrailsQ.data && (
        <GuardrailForm
          value={guardrailsQ.data}
          busy={saveGuardrails.isPending}
          onSave={(patch) => {
            setSaved(null);
            saveGuardrails.mutate(patch);
          }}
        />
      )}

      {generationQ.data && (
        <GenerationForm
          value={generationQ.data}
          busy={saveGeneration.isPending}
          onSave={(patch) => {
            setSaved(null);
            saveGeneration.mutate(patch);
          }}
        />
      )}

      <PhoneNumbers />

      <MachineTokens />
    </div>
  );
}

type Phone = {
  id: number;
  number: string;
  label: string;
  active: boolean;
  position: number;
};

/** The call-tracking numbers that go on ads.
 *
 * These lived in `reference.py` as a Python list, so adding one meant a code
 * change and a redeploy — for something that changes when a campaign changes.
 *
 * Retiring is the normal way to stop using a number: drafts already carrying it
 * keep it, so the record of what published under which number stays intact.
 * Delete is for one added by mistake.
 */
function PhoneNumbers() {
  const qc = useQueryClient();
  const [number, setNumber] = useState("");
  const [label, setLabel] = useState("");
  const [editing, setEditing] = useState<Record<number, string>>({});

  const q = useQuery({
    queryKey: ["reference", "phones"],
    queryFn: () => api.get<{ phones: Phone[] }>("/reference/phones"),
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["reference"] });
    // The composer reads this list; a stale copy would offer a retired number.
    void qc.invalidateQueries({ queryKey: ["locations"] });
  };

  const add = useMutation({
    mutationFn: () => api.post("/reference/phones", { number, label }),
    onSuccess: () => {
      setNumber("");
      setLabel("");
      refresh();
    },
  });
  const patch = useMutation({
    mutationFn: ({ id, ...body }: { id: number } & Partial<Phone>) =>
      api.patch(`/reference/phones/${id}`, body),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/reference/phones/${id}`),
    onSuccess: refresh,
  });

  const phones = q.data?.phones ?? [];
  const activeCount = phones.filter((p) => p.active).length;
  const err = add.error ?? patch.error ?? remove.error ?? q.error;
  const errorText = err ? (err instanceof ApiError ? err.message : String(err)) : null;
  const busy = add.isPending || patch.isPending || remove.isPending;

  return (
    <Section
      title="Phone numbers"
      description="The call-tracking numbers that go on ads. New drafts rotate across the active ones, least-used first, so a number you add here goes into circulation immediately."
    >
      {errorText && (
        <p role="alert" className="text-sm text-danger-fg">
          {errorText}
        </p>
      )}

      {activeCount === 0 && phones.length > 0 && (
        <p className="text-sm text-warn-fg border border-warn-border bg-warn/60 rounded px-2 py-1.5">
          Every number is retired. New drafts fall back to whichever number their
          seed row carries, and the composer offers none.
        </p>
      )}

      <ul className="divide-y divide-border">
        {phones.map((p) => {
          const draft = editing[p.id];
          const changed = draft !== undefined && draft !== p.number;
          return (
            <li key={p.id} className="py-2 flex items-center gap-2 flex-wrap">
              <input
                value={draft ?? p.number}
                onChange={(e) => setEditing({ ...editing, [p.id]: e.target.value })}
                className={cn(
                  "bg-bg border border-border-strong rounded px-2 py-1 text-sm w-40",
                  !p.active && "opacity-50 line-through",
                )}
              />
              <input
                value={p.label}
                placeholder="label (optional)"
                onChange={(e) => patch.mutate({ id: p.id, label: e.target.value })}
                className="bg-bg border border-border-strong rounded px-2 py-1 text-sm w-40 text-fg-muted"
              />
              {changed && (
                <button
                  disabled={busy}
                  onClick={() => {
                    patch.mutate({ id: p.id, number: draft });
                    setEditing((e) => {
                      const { [p.id]: _drop, ...rest } = e;
                      return rest;
                    });
                  }}
                  className="text-xs px-2 py-1 rounded bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
                >
                  Save
                </button>
              )}
              <button
                disabled={busy}
                onClick={() => patch.mutate({ id: p.id, active: !p.active })}
                className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-surface-2 disabled:opacity-40 ml-auto"
              >
                {p.active ? "Retire" : "Reactivate"}
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  if (
                    confirm(
                      `Delete ${p.number}? Retiring keeps it on record instead. ` +
                        `Drafts already using it keep it either way.`,
                    )
                  )
                    remove.mutate(p.id);
                }}
                className="text-xs px-2 py-1 rounded border border-danger-border text-danger-fg hover:bg-danger disabled:opacity-40"
              >
                Delete
              </button>
            </li>
          );
        })}
      </ul>

      <div className="flex gap-2 flex-wrap pt-1">
        <input
          value={number}
          onChange={(e) => setNumber(e.target.value)}
          placeholder="(954) 555-0123"
          className="bg-bg border border-border-strong rounded px-2 py-1.5 text-sm w-44"
        />
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="label (optional)"
          className="bg-bg border border-border-strong rounded px-2 py-1.5 text-sm w-44"
        />
        <button
          disabled={!number.trim() || busy}
          onClick={() => add.mutate()}
          className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
        >
          {add.isPending ? "Adding…" : "Add number"}
        </button>
      </div>
      <p className="text-xs text-fg-subtle">
        Written exactly as you type it — the format is part of how a number is
        recognised in a call log, so it is never reformatted. Retiring one leaves
        every existing draft untouched; change a queued draft's number in Review.
      </p>
    </Section>
  );
}

function Section({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-surface/50">
      <div className="p-3 sm:p-4 border-b border-border">
        <h2 className="font-medium">{title}</h2>
        {description && <p className="text-xs text-fg-subtle mt-1">{description}</p>}
      </div>
      <div className="p-3 sm:p-4 space-y-4">{children}</div>
      {footer && (
        <div className="p-3 sm:p-4 border-t border-border flex flex-wrap items-center gap-3">
          {footer}
        </div>
      )}
    </section>
  );
}

function NumberField({
  label,
  hint,
  ceiling,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint: string;
  /** Text describing the compiled desktop limit, shown always. */
  ceiling?: string;
  value: number;
  min: number;
  max: number;
  onChange: (n: number) => void;
}) {
  const atLimit = value >= max;
  return (
    <label className="block">
      <span className="text-sm text-fg">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => {
          const n = Number.parseInt(e.target.value, 10);
          if (Number.isFinite(n)) onChange(Math.max(min, Math.min(max, n)));
        }}
        className="w-full sm:w-32 mt-1 block bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
      />
      <span className="text-xs text-fg-subtle mt-1 block">{hint}</span>
      {ceiling && (
        <span
          className={cn(
            "text-xs mt-0.5 flex items-center gap-1",
            atLimit ? "text-warn-fg" : "text-fg-subtle",
          )}
        >
          {atLimit && <AlertTriangle size={12} aria-hidden="true" />}
          {ceiling}
        </span>
      )}
    </label>
  );
}

// How big the queue should be, and whether it refills itself.
//
// These two settings were previously split across two forms with two separate
// Save buttons: the depths sat in Guardrails next to the posting caps, and the
// on/off switch was a checkbox buried in Draft generation between an API-key
// warning and the model picker. They are one decision — how many drafts to keep
// and who writes them — so they belong in one place.
//
// The switch applies immediately rather than waiting for a Save, matching the
// posting switch on Review. Ticking a box and navigating away without noticing
// an unsaved form is exactly the mistake worth designing out when the setting
// silently writes AI drafts on a 30-minute loop.
function QueueSizeSection({
  guardrails,
  generation,
  busy,
  onSaveDepth,
  onToggleGeneration,
}: {
  guardrails: Guardrails;
  generation: Generation;
  busy: boolean;
  onSaveDepth: (patch: Partial<Guardrails>) => void;
  onToggleGeneration: (enabled: boolean) => void;
}) {
  const [target, setTarget] = useState(guardrails.queue_depth_target);
  const [floor, setFloor] = useState(guardrails.queue_depth_floor);
  useEffect(() => {
    setTarget(guardrails.queue_depth_target);
    setFloor(guardrails.queue_depth_floor);
  }, [guardrails.queue_depth_target, guardrails.queue_depth_floor]);

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<{ accounts: string[] }>("/accounts"),
    staleTime: 5 * 60_000,
  });
  const list = accountsQ.data?.accounts ?? [];
  const healthQ = useQuery({
    queryKey: ["drafts", "health", list],
    queryFn: () =>
      api.get<{ accounts: Record<string, { queue_depth: number }> }>("/drafts/health", {
        accounts: list.join(","),
      }),
    enabled: list.length > 0,
  });

  const depths = healthQ.data?.accounts ?? {};
  const dirty =
    target !== guardrails.queue_depth_target || floor !== guardrails.queue_depth_floor;
  const invalid = floor > target;
  const on = generation.enabled;

  return (
    <Section
      title="Queue size"
      description="How many drafts to keep waiting per account, and whether the server writes new ones automatically."
      footer={
        <>
          <button
            disabled={!dirty || busy || invalid}
            onClick={() => onSaveDepth({ queue_depth_target: target, queue_depth_floor: floor })}
            className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save queue size"}
          </button>
          {dirty && (
            <button
              onClick={() => {
                setTarget(guardrails.queue_depth_target);
                setFloor(guardrails.queue_depth_floor);
              }}
              className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
            >
              Reset
            </button>
          )}
        </>
      }
    >
      {/* Immediate-apply switch. Same shape as the posting switch on Review, so
          "a thing that is currently on or off" reads the same way everywhere. */}
      <section
        className={cn(
          "rounded border p-3",
          on ? "border-border bg-surface/50" : "border-warn-border bg-warn",
        )}
      >
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            {on ? (
              <div className="flex items-center gap-2">
                <span className="h-2 w-2 rounded-full bg-ok-solid" />
                <span className="text-sm text-fg-muted">
                  Auto-generation is on — topping up to {guardrails.queue_depth_target} per
                  account
                </span>
              </div>
            ) : (
              <>
                <p className="font-semibold text-warn-fg">Auto-generation is off</p>
                <p className="text-xs text-warn-fg/70 mt-0.5">
                  Nothing new is written. The queue only shrinks from here, and an
                  empty queue posts nothing — posting is fail-closed.
                </p>
              </>
            )}
          </div>
          <button
            disabled={busy}
            onClick={() => onToggleGeneration(!on)}
            className={cn(
              "px-4 py-1.5 rounded text-sm shrink-0 inline-flex items-center gap-1.5 disabled:opacity-40",
              on ? "bg-warn-solid hover:bg-warn-solid/90" : "bg-ok-solid hover:bg-ok-solid/90",
            )}
          >
            {on ? <Pause size={14} /> : <Play size={14} />}
            {on ? "Turn off" : "Turn on"}
          </button>
        </div>
      </section>

      <div className="grid gap-4 sm:grid-cols-2">
        <NumberField
          label="Keep per account"
          hint="Top-up fills each account's queue to this many drafts."
          value={target}
          min={1}
          max={1000}
          onChange={setTarget}
        />
        <NumberField
          label="Refill when below"
          hint="Nothing is written until an account drops under this, so drafts arrive in batches rather than one at a time."
          value={floor}
          min={0}
          max={500}
          onChange={setFloor}
        />
      </div>

      {invalid && (
        <p className="text-sm text-danger-fg">
          "Refill when below" cannot exceed "keep per account" — the server rejects
          this.
        </p>
      )}

      {/* Current vs target. Setting a target of 6 while sitting on 15 is a
          normal thing to do; not showing the gap makes it look like nothing
          happened, because top-up only ever adds. */}
      {list.length > 0 && (
        <div className="rounded border border-border p-3">
          <p className="text-xs text-fg-subtle mb-2">Queued right now</p>
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            {list.map((a) => {
              const n = depths[a]?.queue_depth ?? 0;
              return (
                <span key={a} className="text-sm">
                  <span className="text-fg-muted">{a}</span>{" "}
                  <span
                    className={cn(
                      "font-medium",
                      n === 0 ? "text-danger-fg" : n > target ? "text-warn-fg" : "text-fg",
                    )}
                  >
                    {n}
                  </span>
                  <span className="text-fg-subtle"> / {target}</span>
                </span>
              );
            })}
          </div>
          {Object.values(depths).some((d) => d.queue_depth > target) && (
            <p className="text-xs text-fg-subtle mt-2">
              Some accounts hold more than the target. Top-up only adds — lowering
              the number never deletes anything. Trim the extras in{" "}
              <a href="/review" className="underline">
                Review
              </a>
              .
            </p>
          )}
        </div>
      )}
    </Section>
  );
}

function GuardrailForm({
  value,
  busy,
  onSave,
}: {
  value: Guardrails;
  busy: boolean;
  onSave: (patch: Partial<Guardrails>) => void;
}) {
  const [f, setF] = useState(value);
  useEffect(() => setF(value), [value]);

  const set = <K extends keyof Guardrails>(k: K, v: Guardrails[K]) => setF({ ...f, [k]: v });
  const dirty = JSON.stringify(f) !== JSON.stringify(value);
  const windowInvalid = f.post_window_start_hour >= f.post_window_end_hour;

  return (
    <Section
      title="Guardrails"
      description="How often anything may post. The server decides; each desktop clamps whatever it receives to limits compiled into its own config."
      footer={
        <>
          <button
            disabled={!dirty || busy || windowInvalid}
            onClick={() => onSave(f)}
            className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save guardrails"}
          </button>
          {dirty && (
            <button
              onClick={() => setF(value)}
              className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
            >
              Reset
            </button>
          )}
          <span className="text-xs text-fg-subtle">
            Takes effect on the next claim. A post already in flight finishes.
          </span>
        </>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <NumberField
          label="Posts per day, all accounts"
          hint="Counted over a rolling 24 hours, not a calendar day."
          ceiling={`Desktop refuses anything above ${LIMITS.maxPostsPerDay}.`}
          value={f.max_posts_per_day_total}
          min={1}
          max={LIMITS.maxPostsPerDay}
          onChange={(n) => set("max_posts_per_day_total", n)}
        />
        <NumberField
          label="Posts per account, per week"
          hint="Rolling 7 days."
          ceiling={`Desktop refuses anything above ${LIMITS.maxPostsPerWeek}.`}
          value={f.max_posts_per_account_per_week}
          min={1}
          max={LIMITS.maxPostsPerWeek}
          onChange={(n) => set("max_posts_per_account_per_week", n)}
        />
        <NumberField
          label="Hours between posts on one account"
          hint="Cooldown measured from that account's last successful post."
          ceiling={`Desktop raises anything below ${LIMITS.minCooldownHours} back up.`}
          value={f.min_hours_between_posts_same_account}
          min={LIMITS.minCooldownHours}
          max={168}
          onChange={(n) => set("min_hours_between_posts_same_account", n)}
        />
        <div className="grid grid-cols-2 gap-3">
          <NumberField
            label="Window opens (ET)"
            hint="Earliest hour."
            value={f.post_window_start_hour}
            min={LIMITS.earliestHour}
            max={23}
            onChange={(n) => set("post_window_start_hour", n)}
          />
          <NumberField
            label="Window closes (ET)"
            hint={`No earlier than ${LIMITS.earliestHour}:00, no later than ${LIMITS.latestHour}:00.`}
            value={f.post_window_end_hour}
            min={1}
            max={LIMITS.latestHour}
            onChange={(n) => set("post_window_end_hour", n)}
          />
        </div>
      </div>

      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={f.post_weekdays_only}
          onChange={(e) => set("post_weekdays_only", e.target.checked)}
          className="mt-0.5"
        />
        <span>
          <span className="text-sm text-fg block">Weekdays only</span>
          <span className="text-xs text-fg-subtle">
            Nothing posts on Saturday or Sunday, Eastern time.
          </span>
        </span>
      </label>

      {windowInvalid && (
        <p className="text-sm text-danger-fg">
          The window must open before it closes — the server rejects this.
        </p>
      )}
    </Section>
  );
}

function GenerationForm({
  value,
  busy,
  onSave,
}: {
  value: Generation;
  busy: boolean;
  onSave: (patch: Partial<Generation>) => void;
}) {
  const [f, setF] = useState(value);
  useEffect(() => setF(value), [value]);
  const dirty = JSON.stringify(f) !== JSON.stringify(value);
  const rangeInvalid =
    f.photos_min > f.photos_max || f.image_stack_floor > f.image_stack_target;

  return (
    <Section
      title="Draft generation"
      description="How auto-generated copy is written. Whether it runs at all is set under Queue size; the prompt text lives in the Prompt studio."
      footer={
        <>
          <button
            disabled={!dirty || busy || rangeInvalid}
            // `enabled` is deliberately not sent: it is owned by the Queue size
            // switch above and applies immediately. Including it here would let
            // a stale copy of this form silently flip it back on Save.
            onClick={() =>
              onSave({
                model: f.model,
                temperature: f.temperature,
                photos_min: f.photos_min,
                photos_max: f.photos_max,
                imageless_rate: f.imageless_rate,
                image_topup_enabled: f.image_topup_enabled,
                image_stack_floor: f.image_stack_floor,
                image_stack_target: f.image_stack_target,
                image_topup_batch: f.image_topup_batch,
              })
            }
            className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save generation"}
          </button>
          {dirty && (
            <button
              onClick={() => setF(value)}
              className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
            >
              Reset
            </button>
          )}
        </>
      }
    >
      {!value.api_key_configured && (
        <p className="rounded border border-warn-border bg-warn px-3 py-2 text-sm text-warn-fg">
          No MiniMax API key is configured, so every draft uses the workbook copy
          verbatim. That copy repeats across accounts, which is the documented
          cause of ghosting. Set MINIMAX_API_KEY on the server to enable AI copy.
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block">
          <span className="text-sm text-fg">Model</span>
          <input
            value={f.model}
            onChange={(e) => setF({ ...f, model: e.target.value })}
            className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
          />
          <span className="text-xs text-fg-subtle mt-1 block">
            Sent to {value.api_base}
          </span>
        </label>
        <label className="block">
          <span className="text-sm text-fg">Temperature</span>
          <input
            type="number"
            step="0.1"
            min={0}
            max={2}
            value={f.temperature}
            onChange={(e) => {
              const n = Number.parseFloat(e.target.value);
              if (Number.isFinite(n)) setF({ ...f, temperature: Math.max(0, Math.min(2, n)) });
            }}
            className="w-full sm:w-32 mt-1 block bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
          />
          <span className="text-xs text-fg-subtle mt-1 block">
            Higher wanders more. 0–2.
          </span>
        </label>
        <NumberField
          label="Fewest photos per post"
          hint="Craigslist allows up to 24."
          value={f.photos_min}
          min={0}
          max={24}
          onChange={(n) => setF({ ...f, photos_min: n })}
        />
        <NumberField
          label="Most photos per post"
          hint="Photos fill slots 2-24; slot 1 is the cover you pick."
          value={f.photos_max}
          min={0}
          max={24}
          onChange={(n) => setF({ ...f, photos_max: n })}
        />
      </div>

      {rangeInvalid && (
        <p className="text-sm text-danger-fg">
          Fewest cannot exceed most — the server rejects this.
        </p>
      )}

      {/* Off by default: this is the one thing here that spends money on its
          own, and the image prompts are still being tuned. */}
      <div className="pt-3 border-t border-border space-y-3">
        <label className="flex items-start gap-2">
          <input
            type="checkbox"
            checked={f.image_topup_enabled}
            onChange={(e) => setF({ ...f, image_topup_enabled: e.target.checked })}
            className="mt-1"
          />
          <span>
            <span className="text-sm text-fg">Refill the photo stack automatically</span>
            <span className="text-xs text-fg-subtle mt-0.5 block">
              Generates photos into Available whenever depth drops below the floor.
              Covers are never auto-generated — those stay yours to approve. At
              ~$0.0035 an image, 23-photo posts run roughly $7/month. Leave this off
              until the image prompts are settled.
            </span>
          </span>
        </label>
        <div className="grid gap-3 sm:grid-cols-3">
          <NumberField
            label="Refill below"
            hint="Available photos that trigger a refill."
            value={f.image_stack_floor}
            min={0}
            max={100000}
            onChange={(n) => setF({ ...f, image_stack_floor: n })}
          />
          <NumberField
            label="Refill up to"
            hint="Target depth."
            value={f.image_stack_target}
            min={0}
            max={100000}
            onChange={(n) => setF({ ...f, image_stack_target: n })}
          />
          <NumberField
            label="Per cycle"
            hint="Cap per run, so a flip cannot spend it all at once."
            value={f.image_topup_batch}
            min={1}
            max={100}
            onChange={(n) => setF({ ...f, image_topup_batch: n })}
          />
        </div>
        {f.image_stack_floor > f.image_stack_target && (
          <p className="text-sm text-danger-fg">
            The floor cannot exceed the target — the server rejects this.
          </p>
        )}
      </div>

      <p className="text-xs text-fg-subtle">
        {value.seed_ads} seed ads · {value.generated_total} AI / {value.fallback_total} fallback
        {value.last_error ? ` · last error: ${value.last_error}` : ""}
      </p>
    </Section>
  );
}

type Token = {
  id: number;
  machine: string;
  label: string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
};

function MachineTokens() {
  const qc = useQueryClient();
  const [machine, setMachine] = useState("");
  const [label, setLabel] = useState("");
  const [issued, setIssued] = useState<string | null>(null);

  const tokensQ = useQuery({
    queryKey: ["settings", "machine-tokens"],
    queryFn: () => api.get<{ tokens: Token[] }>("/settings/machine-tokens"),
  });

  const create = useMutation({
    mutationFn: () => api.post<{ token: string }>("/settings/machine-tokens", { machine, label }),
    onSuccess: (r) => {
      setIssued(r.token);
      setMachine("");
      setLabel("");
      void qc.invalidateQueries({ queryKey: ["settings", "machine-tokens"] });
    },
  });

  const revoke = useMutation({
    mutationFn: (id: number) => api.del(`/settings/machine-tokens/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings", "machine-tokens"] }),
  });

  const tokens = tokensQ.data?.tokens ?? [];

  // A revoke that silently failed would leave you believing a compromised
  // machine had been cut off when it had not.
  const tokenErr = create.error ?? revoke.error ?? tokensQ.error;
  const tokenErrText = tokenErr
    ? tokenErr instanceof ApiError
      ? tokenErr.message
      : String(tokenErr)
    : null;

  return (
    <Section
      title="Machine access"
      description="One token per Windows machine. This is what lets a desktop claim drafts and download images; without one it posts nothing."
    >
      <p className="rounded border border-warn-border bg-warn px-3 py-2 text-xs text-warn-fg">
        There is one login for this dashboard, so this section is not access
        controlled — anyone who can sign in can issue or revoke a machine token.
        Treat the password accordingly.
      </p>

      {tokenErrText && (
        <p
          role="alert"
          className="rounded border border-danger-border bg-danger px-3 py-2 text-sm text-danger-fg"
        >
          {tokenErrText}
        </p>
      )}

      {issued && (
        <div className="rounded border border-ok-border bg-ok p-3 space-y-2">
          <p className="text-sm font-medium text-ok-fg">
            Copy this now — it is never shown again.
          </p>
          <code className="block break-all rounded bg-bg border border-border-strong px-2 py-1.5 text-xs font-mono">
            {issued}
          </code>
          <p className="text-xs text-ok-fg">
            Put it in the machine's .env as <code>MACHINE_TOKEN</code>, alongside{" "}
            <code>QUEUE_URL</code>.
          </p>
          <button
            onClick={() => setIssued(null)}
            className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-surface-2"
          >
            Done
          </button>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <label className="block">
          <span className="text-xs text-fg-muted">Machine name</span>
          <input
            value={machine}
            onChange={(e) => setMachine(e.target.value)}
            placeholder="desktop-dc320ra"
            className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs text-fg-muted">Label (optional)</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="office PC"
            className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
          />
        </label>
        <button
          disabled={!machine.trim() || create.isPending}
          onClick={() => create.mutate()}
          className="px-3 py-1.5 rounded text-sm bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
        >
          {create.isPending ? "Issuing…" : "Issue token"}
        </button>
      </div>

      {tokens.length === 0 ? (
        <p className="text-sm text-fg-subtle">
          No machines registered. Until one is, nothing can claim a draft.
        </p>
      ) : (
        <ul className="divide-y divide-border rounded border border-border">
          {tokens.map((t) => (
            <li key={t.id} className="p-3 flex flex-wrap items-center gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">
                  {t.machine}
                  {t.label && <span className="text-fg-subtle font-normal"> · {t.label}</span>}
                </p>
                <p className="text-xs text-fg-subtle">
                  issued {formatDateTime(t.created_at)} · last seen{" "}
                  {t.last_seen_at ? formatDateTime(t.last_seen_at) : "never"}
                </p>
              </div>
              {t.revoked_at ? (
                <span className="text-xs px-2 py-0.5 rounded bg-surface-2 text-fg-muted">
                  revoked
                </span>
              ) : (
                <button
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(t.id)}
                  className="text-xs px-2 py-1 rounded border border-danger-border text-danger-fg hover:bg-danger disabled:opacity-40"
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
