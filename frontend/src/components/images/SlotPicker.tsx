// The image slots of one ad, and the picker for filling them.
//
// Shared by queued drafts and live postings. The two differ only in which URLs
// they talk to, so the target is an endpoint descriptor rather than a
// `kind: "draft" | "post"` union — a union would force a switch at every one of
// the six call sites in here, and would push the backend toward normalising two
// route shapes that are fine as they are.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { cn } from "../../lib/cn";

// Craigslist accepts 24 images per posting: one thumbnail plus 23 more.
export const MAX_IMAGE_SLOTS = 24;

// Slot 1 is the thumbnail and takes a cover; every other slot takes a photo.
// The server enforces this — attaching the wrong kind is a 409.
export const COVER_SLOT = 1;

const IMG_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

// A candidate from one of the two stacks. `bucket` is computed server-side;
// 'assigned' means a live draft or a live posting already holds it.
export type PoolImage = {
  id: number;
  kind: "cover" | "photo";
  bucket: string;
  assigned_draft_id: number | null;
};

export type ImageTarget = {
  /** Identifies the holder, for self-hold checks: "draft:12" | "post:7811111111". */
  key: string;
  account: string;
  list: string;
  attach: string;
  detach: (imageId: number) => string;
  autofill: string;
  cover: string;
};

export const draftTarget = (draftId: number, account: string): ImageTarget => ({
  key: `draft:${draftId}`,
  account,
  list: `/images/draft/${draftId}`,
  attach: `/images/draft/${draftId}/attach`,
  detach: (id) => `/images/draft/${draftId}/attach/${id}`,
  autofill: `/images/draft/${draftId}/autofill`,
  cover: `/images/draft/${draftId}/cover`,
});

export const postTarget = (postId: string, account: string): ImageTarget => ({
  key: `post:${postId}`,
  account,
  list: `/edits/${postId}/images`,
  attach: `/edits/${postId}/images`,
  detach: (id) => `/edits/${postId}/images/${id}`,
  autofill: `/edits/${postId}/images/autofill`,
  cover: `/edits/${postId}/images/cover`,
});

export function SlotPicker(props: {
  target: ImageTarget;
  busy: boolean;
  /**
   * Called after any change. On a live posting an image change bumps
   * `desired_rev` server-side, so the caller has to refetch or its
   * "pending rev N → M" badge goes stale the moment you attach a photo.
   */
  onChanged?: () => void;
  /** Copy shown when nothing is attached; differs for a live ad. */
  emptyNote?: string;
}) {
  const { target } = props;
  const [attached, setAttached] = useState<{ id: number; slot: number }[]>([]);
  const [pool, setPool] = useState<PoolImage[]>([]);
  // Which stack the open picker is showing: covers for slot 1, photos for the
  // rest. `null` means closed.
  const [picking, setPicking] = useState<"cover" | "photo" | null>(null);
  const [filling, setFilling] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ images: { id: number; slot: number }[] }>(target.list);
      setAttached(r.images);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }, [target.list]);

  useEffect(() => {
    void load();
  }, [load]);

  async function openPicker(kind: "cover" | "photo") {
    setErr(null);
    try {
      // Reserved images come back too, so they can be offered greyed rather
      // than vanishing with no explanation of where they went.
      const r = await api.get<{ images: PoolImage[] }>("/images", {
        status: "approved",
        kind,
        account: target.account,
        limit: 60,
      });
      setPool(r.images);
      setPicking(kind);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function act(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
      await load();
      props.onChanged?.();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function autofill() {
    setErr(null);
    setNote(null);
    setFilling(true);
    try {
      const r = await api.post<{ filled: number; requested: number }>(target.autofill, {
        count: MAX_IMAGE_SLOTS - 1,
      });
      // A short fill is the ordinary case with manual refill, so it is reported
      // plainly rather than as an error.
      setNote(
        r.filled === 0
          ? "Nothing to add — the photo stack has nothing free for this account."
          : r.filled < r.requested
            ? `Filled ${r.filled} of ${r.requested} — the photo stack is short.`
            : `Filled ${r.filled} slots.`,
      );
      await load();
      props.onChanged?.();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setFilling(false);
    }
  }

  const cover = attached.find((a) => a.slot === COVER_SLOT) ?? null;
  const photos = attached.filter((a) => a.slot !== COVER_SLOT);
  // Photos land in the first free slot after the cover, so a detach in the
  // middle is reused rather than pushing everything toward the 24-slot ceiling.
  const taken = new Set(attached.map((a) => a.slot));
  const nextPhotoSlot =
    Array.from({ length: MAX_IMAGE_SLOTS - 1 }, (_, i) => i + 2).find((s) => !taken.has(s)) ??
    MAX_IMAGE_SLOTS;
  const targetSlot = picking === "cover" ? COVER_SLOT : nextPhotoSlot;
  const free = pool.filter((p) => !attached.some((a) => a.id === p.id));

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-fg-muted">
          Images ({attached.length}/{MAX_IMAGE_SLOTS})
        </span>
        <button
          disabled={props.busy}
          onClick={() => void openPicker("cover")}
          className={cn(
            "text-xs px-2 py-0.5 rounded border disabled:opacity-40",
            cover
              ? "border-border-strong text-fg-muted hover:bg-surface-2"
              : "border-warn-border bg-warn text-warn-fg hover:opacity-90",
          )}
        >
          {cover ? "Change cover" : "Choose cover"}
        </button>
        {photos.length < MAX_IMAGE_SLOTS - 1 && (
          <>
            <button
              disabled={props.busy || filling}
              onClick={() => void autofill()}
              className="text-xs px-2 py-0.5 rounded border border-border-strong text-fg-muted hover:bg-surface-2 disabled:opacity-40"
            >
              {filling ? "Filling…" : `Autofill ${MAX_IMAGE_SLOTS - 1} photos`}
            </button>
            <button
              disabled={props.busy}
              onClick={() => void openPicker("photo")}
              className="text-xs px-2 py-0.5 rounded border border-border-strong text-fg-muted hover:bg-surface-2 disabled:opacity-40"
            >
              + Add photo
            </button>
          </>
        )}
      </div>
      {err && <p className="text-xs text-danger-fg">{err}</p>}
      {note && <p className="text-xs text-fg-muted">{note}</p>}
      {!cover && attached.length > 0 && (
        <p className="text-xs text-warn-fg">
          No cover chosen — whichever image lands first becomes the Craigslist
          thumbnail.
        </p>
      )}

      {attached.length === 0 ? (
        <p className="text-xs text-fg-subtle italic">
          {props.emptyNote ?? "No images — this post will go out text-only."}
        </p>
      ) : (
        <ul className="flex gap-2 flex-wrap">
          {attached.map((a) => (
            <li key={a.id} className="relative">
              {/* /thumb. Stored files average ~772KB, so an ad at the 24-slot
                  limit was ~18MB of full-resolution downloads the moment you
                  opened it, on whatever connection you were on. Lazy + async
                  decode + explicit dimensions means only what you scroll to. */}
              <img
                src={`${IMG_BASE}/images/${a.id}/thumb`}
                alt={
                  a.slot === COVER_SLOT
                    ? "Cover image (Craigslist thumbnail)"
                    : `Image in slot ${a.slot}`
                }
                loading="lazy"
                decoding="async"
                width={112}
                height={80}
                className="h-20 w-28 object-cover rounded border border-border-strong bg-surface-2"
              />
              <span className="absolute top-0.5 left-0.5 text-[10px] px-1 rounded bg-black/70 text-white">
                {a.slot === COVER_SLOT ? "cover" : a.slot}
              </span>
              <button
                onClick={() => act(() => api.del(target.detach(a.id)))}
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
              {picking === "cover"
                ? `Cover stack — becomes the Craigslist thumbnail`
                : `Photo stack — attaches as slot ${targetSlot}`}
              {" · "}
              {target.account}
            </span>
            <button
              onClick={() => setPicking(null)}
              className="text-xs text-fg-muted hover:text-fg px-1"
            >
              close
            </button>
          </div>
          {free.length === 0 ? (
            <p className="text-xs text-fg-subtle">
              The {picking} stack is empty for this account. Generate or upload{" "}
              {picking}s on the Images page.
            </p>
          ) : (
            <ul className="flex gap-2 flex-wrap max-h-44 overflow-auto">
              {free.map((p) => {
                // Reserved by something else. Shown rather than hidden, because
                // an image silently missing from the picker is impossible to
                // reason about — and reuse is legitimate, just never accidental.
                const heldBy =
                  p.assigned_draft_id !== null ? `draft:${p.assigned_draft_id}` : null;
                const held =
                  p.bucket === "assigned" && heldBy !== target.key
                    ? (p.assigned_draft_id ?? "a live posting")
                    : null;
                return (
                  <li key={p.id}>
                    <button
                      onClick={() =>
                        act(async () => {
                          if (
                            held !== null &&
                            !window.confirm(
                              `Image ${p.id} is already reserved by ` +
                                `${typeof held === "number" ? `draft #${held}` : held}. ` +
                                `Using it here means the same picture goes out twice on ` +
                                `this account, which is what gets listings ghosted.\n\n` +
                                `Attach it anyway?`,
                            )
                          )
                            return;
                          await api.post(target.attach, {
                            image_id: p.id,
                            slot: targetSlot,
                            allow_double_book: held !== null,
                          });
                          setPicking(null);
                        })
                      }
                      className="block relative"
                      title={
                        held !== null
                          ? `Reserved by ${typeof held === "number" ? `draft #${held}` : held} — click to reuse anyway`
                          : `Attach as slot ${targetSlot}`
                      }
                    >
                      <img
                        src={`${IMG_BASE}/images/${p.id}/thumb`}
                        alt={`Image ${p.id}${held !== null ? `, reserved` : ""}`}
                        loading="lazy"
                        decoding="async"
                        width={96}
                        height={64}
                        className={cn(
                          "h-16 w-24 object-cover rounded border border-border-strong bg-surface-2 hover:border-ring",
                          held !== null && "opacity-40",
                        )}
                      />
                      {held !== null && (
                        <span className="absolute bottom-0.5 left-0.5 text-[10px] px-1 rounded bg-black/70 text-warn-fg">
                          {typeof held === "number" ? `#${held}` : "live"}
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
