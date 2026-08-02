// Editing a live posting, from the post's own page.
//
// The whole flow is asynchronous and nothing here happens the moment you click,
// so the panel's main job after collecting the edit is saying so plainly:
//
//   Load       the desktop opens the real Craigslist edit form and reports back
//              what it says. The dashboard has never stored post bodies, so
//              this is the only way to know the current content.
//   Edit       records desired state, not a job. Editing twice before the
//              desktop runs supersedes; it does not queue twice.
//   Reconcile  the desktop takes the browser lease, re-reads the form, checks it
//              still matches what you were shown, and applies the change.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../lib/api";
import { cn } from "../../lib/cn";
import { formatDateTime } from "../../lib/format";
import { Modal } from "../Modal";
import { PostingForm } from "../posting/PostingForm";
import { SlotPicker, postTarget } from "../images/SlotPicker";
import { ArtifactLinks, StepTrail } from "./PostEditHistory";
import {
  effectiveBodyLength,
  postingDirty,
  splitBody,
  POSTING_BODY_LIMIT,
  type LocationRef,
  type PostingFormValue,
} from "../../lib/posting";
import { hasPendingEdit, type EditablePost } from "../../lib/edits";

/**
 * Seed the form from the desired state when one exists, otherwise from the live
 * posting. Seeding from live while a change is pending would show pre-edit text
 * and silently revert the operator's own work on the next save.
 */
function formValue(p: EditablePost): PostingFormValue {
  const staged = p.desired_rev !== null;
  const pick = (desired: string | null, live: string | null) =>
    (staged ? (desired ?? live) : live) ?? "";
  return {
    account: p.account,
    county: p.county ?? "",
    city: pick(p.desired_city, p.city),
    postal_code: pick(p.desired_postal_code, p.postal_code),
    phone_number: pick(p.desired_phone_number, p.phone_number),
    license_number: pick(p.desired_license_number, p.license_number),
    title: pick(p.desired_title, p.title),
    body: pick(p.desired_body, p.body),
    body_head: p.body_head,
    geographic_area: "",
  };
}

export function PostEditPanel(props: {
  post: EditablePost;
  accounts: string[];
  locations: LocationRef | null;
  onError: (message: string) => void;
}) {
  const p = props.post;
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  // What the server said when the request was accepted. A 202 with blocks means
  // it was recorded but will not run until they clear, and saying so at the
  // click beats letting it expire twenty minutes later.
  const [blocks, setBlocks] = useState<string[] | null>(null);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["edits", p.post_id] });
  };

  const run = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: refresh,
    onError: (e) => props.onError(e instanceof ApiError ? e.message : String(e)),
  });

  const loading = !!p.hydrate_requested_at;
  const loaded = !!p.hydrated_at;
  const applying = p.edit_status === "applying";
  const pending = hasPendingEdit(p);

  return (
    <section className="rounded-lg border border-border bg-surface p-4 space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="font-semibold">Ad copy</h2>
          <p className="text-xs text-fg-muted">
            {loaded ? (
              <>Loaded from Craigslist {formatDateTime(p.hydrated_at)}</>
            ) : (
              "Never loaded from Craigslist"
            )}
            {pending && (
              <>
                {" · "}
                <span className="text-accent-fg">
                  change pending, rev {p.live_rev} → {p.desired_rev}
                </span>
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            disabled={run.isPending || loading}
            onClick={() => run.mutate(() => api.post(`/edits/${p.post_id}/hydrate`))}
            className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-bg disabled:opacity-40"
          >
            {loading ? "Loading…" : loaded ? "Reload from Craigslist" : "Load from Craigslist"}
          </button>
          <button
            disabled={!loaded || applying || run.isPending}
            title={
              !loaded
                ? "Load the post from Craigslist first"
                : applying
                  ? "The desktop is editing this posting right now"
                  : undefined
            }
            onClick={() => setEditing(true)}
            className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-bg disabled:opacity-40"
          >
            Edit
          </button>
          {pending && (
            <>
              <button
                disabled={applying || run.isPending || !!p.reconcile_requested_at}
                title={
                  p.reconcile_requested_at
                    ? "Already asked — the posting machine picks this up within about 15 seconds"
                    : "Apply this change to the live posting now"
                }
                onClick={() =>
                  run.mutate(async () => {
                    const r = await api.post<{ eligible: boolean; blocks: string[] }>(
                      `/edits/${p.post_id}/apply-now`,
                    );
                    setBlocks(r.eligible ? [] : r.blocks);
                    return r;
                  })
                }
                className="text-xs px-2 py-1 rounded bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40"
              >
                {p.reconcile_requested_at ? "Applying soon…" : "Apply now"}
              </button>
              <button
                disabled={applying || run.isPending}
                onClick={() => run.mutate(() => api.del(`/edits/${p.post_id}/desired`))}
                className="text-xs px-2 py-1 rounded border border-border-strong text-danger-fg hover:bg-bg disabled:opacity-40"
              >
                Discard change
              </button>
            </>
          )}
        </div>
      </div>

      {applying && (
        <p className="rounded border border-accent-border bg-accent/10 px-2 py-1.5 text-xs text-accent-fg">
          The posting machine is editing this ad right now — it took the change
          at {formatDateTime(p.last_attempt_at)}. Nothing can be altered until it
          reports back, so the controls above are disabled rather than silently
          refused.
        </p>
      )}

      {p.hydrate_error && (
        <p className="text-xs text-danger-fg">Load failed: {p.hydrate_error}</p>
      )}

      {p.reconcile_request_error && (
        <p className="text-xs text-warn-fg">{p.reconcile_request_error}</p>
      )}

      {!!blocks?.length && (
        <div className="rounded border border-warn-border bg-warn/40 px-2 py-1.5">
          <p className="text-xs text-warn-fg">
            Asked for, but it will not run until these clear:
          </p>
          <ul className="mt-1 list-disc pl-5 text-xs text-warn-fg space-y-0.5">
            {blocks.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
          <p className="mt-1 text-xs text-fg-muted">
            Caps and switches live under Settings → Guardrails. The request
            expires in 20 minutes if nothing picks it up.
          </p>
        </div>
      )}

      {p.reconcile_requested_at && !blocks?.length && (
        <p className="text-xs text-fg-muted">
          Waiting for the posting machine to apply this — it polls every 15
          seconds. Nothing reaches the live ad until it finishes.
        </p>
      )}

      {/* What the last read of the form actually saw. Craigslist's edit form is
          DOM this project inferred rather than observed, so the selector census
          is the first thing worth reading — whether the read succeeded or not.
          A count of 2 is as wrong as a count of 0: the fill helpers take
          `.first`. */}
      {!!(p.hydrate_steps?.length || p.hydrate_artifact_ids?.length) && (
        <details className="rounded border border-border bg-bg/40 p-2">
          <summary className="cursor-pointer text-xs text-fg-muted">
            What the last load saw
          </summary>
          <StepTrail steps={p.hydrate_steps ?? []} />
          <ArtifactLinks ids={p.hydrate_artifact_ids ?? []} />
        </details>
      )}

      {loading && (
        <p className="text-xs text-fg-muted">
          Waiting for the posting machine — it picks this up within about 15
          seconds, then opens Craigslist and reads the form.
        </p>
      )}

      {!loaded && !loading && (
        <p className="text-xs text-fg-subtle">
          The dashboard has never stored what this ad says — only that it was
          published. Load it from Craigslist to see the current copy and edit it.
        </p>
      )}

      {loaded && (
        <>
          <div>
            <div className="text-sm font-medium">{p.desired_title ?? p.title}</div>
            <pre className="mt-1 text-xs text-fg-muted whitespace-pre-wrap font-mono max-h-48 overflow-auto bg-bg/60 rounded p-2">
              {splitBody(p.desired_body ?? p.body ?? "", p.body_head).head}
            </pre>
          </div>

          <div className="border-t border-border pt-3">
            <SlotPicker
              target={postTarget(p.post_id, p.account)}
              busy={applying || run.isPending}
              onChanged={refresh}
              emptyNote={
                p.image_set_managed
                  ? "No images staged — applying this would remove every photo from the live ad."
                  : "Images are not being managed. Attach one to take control of this ad's gallery."
              }
            />
            {p.image_set_managed && (
              <button
                disabled={applying || run.isPending}
                title={
                  applying
                    ? "The posting machine is editing this ad right now"
                    : "Apply the change without touching the live gallery"
                }
                onClick={() =>
                  run.mutate(() =>
                    api.put(`/edits/${p.post_id}/desired`, { image_set_managed: false }),
                  )
                }
                className="mt-2 text-xs text-fg-muted underline hover:text-fg disabled:opacity-40"
              >
                Leave the live images alone
              </button>
            )}
          </div>
        </>
      )}

      {editing && (
        <EditDialog
          post={p}
          accounts={props.accounts}
          locations={props.locations}
          onClose={() => setEditing(false)}
          onSave={async (patch) => {
            await run.mutateAsync(() => api.put(`/edits/${p.post_id}/desired`, patch));
            setEditing(false);
          }}
        />
      )}
    </section>
  );
}

function EditDialog(props: {
  post: EditablePost;
  accounts: string[];
  locations: LocationRef | null;
  onClose: () => void;
  onSave: (patch: Record<string, unknown>) => Promise<void>;
}) {
  const initial = formValue(props.post);
  const [f, setF] = useState<PostingFormValue>(initial);
  const dirty = postingDirty(f, initial);
  const overLimit = effectiveBodyLength(f.body) > POSTING_BODY_LIMIT;

  return (
    <Modal
      open
      onOpenChange={(o) => !o && props.onClose()}
      onRequestClose={() => !dirty || confirm("Discard your unsaved changes to this ad?")}
      title={`Edit live post #${props.post.post_id}`}
      footer={
        <>
          <span className="text-xs text-fg-subtle mr-auto">
            Queues the change. The posting machine applies it when it next has a
            free browser and the edit window is open.
          </span>
          <button
            onClick={props.onClose}
            className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            disabled={!dirty || overLimit}
            title={
              overLimit
                ? "The body is over Craigslist's limit — shorten it before saving"
                : undefined
            }
            onClick={() =>
              void props.onSave({
                title: f.title,
                body: f.body,
                city: f.city,
                postal_code: f.postal_code,
                phone_number: f.phone_number,
                license_number: f.license_number,
              })
            }
            className={cn(
              "px-3 py-1.5 rounded text-sm bg-primary text-primary-fg",
              "hover:bg-primary-hover disabled:opacity-40 disabled:cursor-not-allowed",
            )}
          >
            Queue change
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
          // A live posting belongs to the account that published it, its
          // Craigslist subarea is fixed at post time, and its area box is free
          // text that often names several towns.
          accountEditable: false,
          showCounty: false,
          showGeographicArea: false,
          cityMode: "freetext",
        }}
      />
    </Modal>
  );
}
