// The image stacks.
//
// Two of them, not one. Covers are hand-picked and become the Craigslist
// thumbnail; photos are bulk and fill slots 2-24. They were always separate
// ideas but shared a grid, so a cover with a phone number composited across it
// could be drawn as an ordinary photo and nobody would see it happen.
//
// Within a stack, five buckets that cannot overlap: an image is pending review,
// available, reserved by a queued draft, already published, or rejected.

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { ConfirmDialog } from "../components/Modal";

type Kind = "cover" | "photo";
type Bucket = "pending" | "available" | "assigned" | "published" | "rejected";

type Image = {
  id: number;
  sha256: string;
  mime: string;
  bytes_size: number;
  source: string;
  status: string;
  kind: Kind;
  bucket: Bucket;
  assigned_draft_id: number | null;
  owner_account: string | null;
  prompt: string | null;
  cost_usd: string | null;
  used_at: string | null;
  created_at: string;
};

type StackHealth = {
  queued_drafts: number;
  photos_per_draft: number;
  photo_demand: number;
  photos_available: number;
  photos_short: number;
  covers_available: number;
};

type Stats = {
  by_status: { status: string; kind: string; n: number }[];
  spend_usd: number;
  available: Record<string, number>;
  buckets: Record<Kind, Record<Bucket, number>>;
  stack_health: StackHealth;
  storage: { files: number; bytes: number };
};

const BUCKETS: { key: Bucket; label: string }[] = [
  { key: "pending", label: "Pending review" },
  { key: "available", label: "Available" },
  { key: "assigned", label: "Assigned" },
  { key: "published", label: "Published" },
  { key: "rejected", label: "Rejected" },
];

const EMPTY_COPY: Record<Bucket, { cover: string; photo: string }> = {
  pending: {
    cover: "No covers waiting for review.",
    photo: "No photos waiting for review.",
  },
  available: {
    cover:
      "No covers free. Any draft without a hand-picked cover will publish with a roof photo as its thumbnail.",
    photo: "No photos free — drafts will autofill short.",
  },
  assigned: {
    cover: "No cover is reserved by a queued draft.",
    photo: "No photos are reserved by queued drafts.",
  },
  published: { cover: "No cover has gone out yet.", photo: "No photo has gone out yet." },
  rejected: { cover: "Nothing rejected.", photo: "Nothing rejected." },
};

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";

function mb(bytes: number): string {
  return bytes > 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${Math.round(bytes / 1024)} KB`;
}

export default function ImagesPage() {
  const qc = useQueryClient();
  const [kind, setKind] = useState<Kind>("cover");
  const [bucket, setBucket] = useState<Bucket>("available");
  const [count, setCount] = useState(4);
  const [note, setNote] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const imagesQ = useQuery({
    queryKey: ["images", kind, bucket],
    queryFn: () => api.get<{ images: Image[] }>("/images", { kind, bucket, limit: 120 }),
  });
  const statsQ = useQuery({
    queryKey: ["images", "stats"],
    queryFn: () => api.get<Stats>("/images/stats"),
  });

  const images = imagesQ.data?.images ?? [];
  const stats = statsQ.data ?? null;
  const health = stats?.stack_health ?? null;
  const counts = stats?.buckets?.[kind] ?? null;

  const refresh = () => qc.invalidateQueries({ queryKey: ["images"] });

  // Per-image pending state. A single page-wide `busy` flag used to grey out
  // every button in the grid whenever one image was approved, with nothing to
  // say which one you had acted on.
  const patch = useMutation({
    mutationFn: ({ id, ...body }: { id: number; status?: string; kind?: Kind }) =>
      api.patch(`/images/${id}`, body),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/images/${id}`),
    onSuccess: refresh,
  });

  // Generate and upload inherit the selected stack, so there is no way to
  // generate a cover while looking at photos and wonder where it went.
  const generate = useMutation({
    mutationFn: () =>
      api.post<{ created: number; cost_usd: number; error: string | null }>(
        "/images/generate",
        { count, kind },
      ),
    onSuccess: (r) => {
      // Generation reports provider trouble in the body rather than failing, so
      // surface both outcomes: what arrived, and what went wrong.
      setNote(
        `Generated ${r.created} ${kind}${r.created === 1 ? "" : "s"} · $${r.cost_usd.toFixed(4)}` +
          (r.error ? ` · stopped early: ${r.error}` : ""),
      );
      setBucket("pending");
      void refresh();
    },
  });

  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      let added = 0;
      const skipped: string[] = [];
      for (const f of files) {
        const form = new FormData();
        form.append("file", f);
        const res = await fetch(
          `${BASE.replace(/\/+$/, "")}/images/upload?kind=${encodeURIComponent(kind)}`,
          { method: "POST", credentials: "include", body: form },
        );
        if (res.ok) added++;
        else
          skipped.push(
            `${f.name} (${res.status === 409 ? "already in stack" : `HTTP ${res.status}`})`,
          );
      }
      return { added, skipped };
    },
    onSuccess: ({ added, skipped }) => {
      setNote(
        `Uploaded ${added} file${added === 1 ? "" : "s"} to the ${kind} stack` +
          (skipped.length ? ` · skipped ${skipped.join(", ")}` : ""),
      );
      if (fileRef.current) fileRef.current.value = "";
      setBucket("available");
      void refresh();
    },
  });

  const [deleting, setDeleting] = useState<Image | null>(null);

  /** Only the image being acted on is disabled, not the whole grid. */
  const pendingFor = (id: number) =>
    (patch.isPending && patch.variables?.id === id) ||
    (remove.isPending && remove.variables === id);

  const busy = generate.isPending || upload.isPending;
  const error =
    (imagesQ.error ?? statsQ.error ?? patch.error ?? remove.error ?? generate.error ??
      upload.error) || null;
  const errorText = error
    ? error instanceof ApiError
      ? error.message
      : String(error)
    : null;

  const other: Kind = kind === "cover" ? "photo" : "cover";

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold">Images</h1>
          <div className="flex rounded border border-border-strong overflow-hidden">
            {(["cover", "photo"] as Kind[]).map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={cn(
                  "px-3 py-1 text-sm capitalize",
                  kind === k ? "bg-accent text-accent-fg" : "text-fg-muted hover:bg-surface-2",
                )}
              >
                {k === "cover" ? "Covers" : "Photos"}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            type="number"
            min={1}
            max={20}
            value={count}
            // Number("abc") is NaN, and `NaN || 1` silently became 1 — you
            // asked for something invalid and got a batch you did not choose.
            onChange={(e) => {
              const n = Number.parseInt(e.target.value, 10);
              if (Number.isFinite(n)) setCount(Math.max(1, Math.min(20, n)));
            }}
            aria-label="How many images to generate (1-20)"
            className="w-16 bg-bg border border-border-strong rounded px-2 py-1 text-sm"
          />
          <button
            disabled={busy}
            onClick={() => generate.mutate()}
            className="text-sm px-3 py-1 rounded bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-40"
          >
            {generate.isPending ? "Generating…" : `Generate ${kind}s`}
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            accept="image/jpeg,image/png,image/webp"
            onChange={(e) => {
              const files = e.target.files ? Array.from(e.target.files) : [];
              if (files.length) upload.mutate(files);
            }}
            className="hidden"
            id="upload-input"
          />
          <label
            htmlFor="upload-input"
            className="text-sm px-3 py-1 rounded bg-primary text-primary-fg hover:bg-primary-hover cursor-pointer"
          >
            {upload.isPending ? "Uploading…" : "Upload"}
          </label>
          <button
            onClick={() => void refresh()}
            className="text-sm px-2 py-1 rounded hover:bg-surface-2 text-fg-muted"
          >
            Refresh
          </button>
        </div>
      </div>

      {errorText && (
        <div
          role="alert"
          className="rounded border border-danger-border bg-danger px-3 py-2 text-sm text-danger-fg"
        >
          {errorText}
        </div>
      )}
      {note && (
        <div className="rounded border border-border-strong bg-surface px-3 py-2 text-sm text-fg-muted">
          {note}
        </div>
      )}

      {/* Refill is manual, so being short is the normal state rather than an
          alarm — it is stated continuously instead of raised once. */}
      {health && health.photos_short > 0 && (
        <div className="rounded border border-warn-border bg-warn px-3 py-2 text-sm text-warn-fg">
          <strong>Photo stack short by {health.photos_short}.</strong>{" "}
          {health.queued_drafts} queued draft{health.queued_drafts === 1 ? "" : "s"} want{" "}
          {health.photo_demand} photos, {health.photos_available} available. Drafts fill with
          what exists and publish thinner — nothing is blocked.
        </div>
      )}
      {health && health.covers_available === 0 && (
        <div className="rounded border border-danger-border bg-danger px-3 py-2 text-sm text-danger-fg">
          <strong>Cover stack is empty.</strong> Any draft you have not given a cover will
          publish with an ordinary roof photo as its Craigslist thumbnail.
        </div>
      )}

      {stats && (
        <section className="rounded border border-border bg-surface/50 p-3 text-sm">
          <div className="flex gap-5 flex-wrap text-fg-muted">
            <span>
              Total spend <strong className="text-fg">${stats.spend_usd.toFixed(4)}</strong>
            </span>
            <span>
              Storage {stats.storage.files} files, {mb(stats.storage.bytes)}
            </span>
            {health && (
              <span>
                {health.photos_per_draft} photos/post ×{" "}
                {health.queued_drafts} queued
              </span>
            )}
            {Object.entries(stats.available).map(([acct, n]) => (
              <span key={acct}>
                {acct} <strong className="text-fg">{n}</strong> owned
              </span>
            ))}
          </div>
        </section>
      )}

      <div className="flex gap-1 flex-wrap">
        {BUCKETS.map((b) => (
          <button
            key={b.key}
            onClick={() => setBucket(b.key)}
            className={cn(
              "px-3 py-1.5 rounded text-sm",
              bucket === b.key ? "bg-surface-2 text-fg" : "text-fg-muted hover:bg-surface-2/60",
            )}
          >
            {b.label}
            {counts ? ` ${counts[b.key] ?? 0}` : null}
          </button>
        ))}
      </div>

      {imagesQ.isLoading ? (
        // A skeleton rather than an empty page that pops into content.
        <ul className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-5" aria-hidden="true">
          {Array.from({ length: 10 }).map((_, i) => (
            <li key={i} className="rounded border border-border overflow-hidden">
              <div className="w-full aspect-[4/3] bg-surface-2 animate-pulse" />
              <div className="p-2 space-y-2">
                <div className="h-3 w-2/3 bg-surface-2 rounded animate-pulse" />
                <div className="h-6 w-full bg-surface-2 rounded animate-pulse" />
              </div>
            </li>
          ))}
        </ul>
      ) : images.length === 0 ? (
        <p className="text-fg-subtle text-sm py-10 text-center">
          {EMPTY_COPY[bucket][kind]}
        </p>
      ) : (
        <ul className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-5">
          {images.map((img) => (
            <li
              key={img.id}
              className="rounded border border-border bg-surface/40 overflow-hidden"
            >
              {/* /raw is the full file; a 120-image grid is ~40MB if it all
                  loads. decoding="async" keeps decode off the main thread and
                  the explicit box stops layout shift as each one arrives. */}
              <img
                src={`${BASE.replace(/\/+$/, "")}/images/${img.id}/raw`}
                alt={img.prompt ?? `Image ${img.id}`}
                loading="lazy"
                decoding="async"
                className="w-full aspect-[4/3] object-cover bg-surface-2"
              />
              <div className="p-2 space-y-1.5">
                <div className="flex items-center gap-1 flex-wrap text-xs">
                  <span className="text-fg-subtle">#{img.id}</span>
                  {img.owner_account && (
                    <span className="px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted">
                      {img.owner_account}
                    </span>
                  )}
                  {img.source === "uploaded" && (
                    <span className="px-1.5 py-0.5 rounded bg-info text-info-fg">
                      uploaded
                    </span>
                  )}
                  {/* A live posting's desired set reserves images too, and it
                      has no draft id — say "reserved" rather than "draft #null". */}
                  {img.bucket === "assigned" && (
                    <span className="px-1.5 py-0.5 rounded bg-warn text-warn-fg">
                      {img.assigned_draft_id ? `draft #${img.assigned_draft_id}` : "reserved"}
                    </span>
                  )}
                  {img.bucket === "published" && (
                    <span className="px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted">
                      published
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-fg-subtle">{mb(img.bytes_size)}</p>
                <div className="flex gap-1">
                  {img.status !== "approved" && (
                    <button
                      disabled={pendingFor(img.id)}
                      onClick={() => patch.mutate({ id: img.id, status: "approved" })}
                      className="flex-1 text-xs px-2 py-1 rounded bg-ok-solid/90 text-on-solid hover:bg-ok-solid disabled:opacity-40"
                    >
                      Approve
                    </button>
                  )}
                  {img.status === "pending" && (
                    <button
                      disabled={pendingFor(img.id)}
                      onClick={() => patch.mutate({ id: img.id, status: "rejected" })}
                      className="flex-1 text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-surface-2 disabled:opacity-40"
                    >
                      Reject
                    </button>
                  )}
                  {img.status !== "pending" && (
                    <button
                      disabled={pendingFor(img.id)}
                      onClick={() => setDeleting(img)}
                      className="flex-1 text-xs px-2 py-1 rounded border border-danger-border text-danger-fg hover:bg-danger disabled:opacity-40"
                    >
                      Delete
                    </button>
                  )}
                </div>
                {/* The escape hatch that makes the hard partition liveable: a
                    generated shot that would make a good thumbnail is one click
                    from being usable as one. Refused while a live draft holds
                    it, since the slot it sits in is validated against its kind. */}
                <button
                  disabled={pendingFor(img.id) || img.bucket === "assigned"}
                  onClick={() => patch.mutate({ id: img.id, kind: other })}
                  title={
                    img.bucket === "assigned"
                      ? `Detach it from draft #${img.assigned_draft_id} first`
                      : `Move to the ${other} stack`
                  }
                  className="w-full text-[11px] px-2 py-1 rounded text-fg-subtle hover:bg-surface-2 hover:text-fg disabled:opacity-30"
                >
                  Make this a {other}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete image #${deleting?.id}?`}
        body={
          <>
            {deleting?.used_at ? (
              <span className="block text-warn-fg">
                This image has already been published to Craigslist. Deleting it
                removes the record of what went out.
              </span>
            ) : (
              <span className="block">The file is removed from storage permanently.</span>
            )}
            <span className="block mt-1">This cannot be undone.</span>
          </>
        }
        busy={remove.isPending}
        onConfirm={() => {
          const id = deleting?.id;
          setDeleting(null);
          if (id !== undefined) remove.mutate(id);
        }}
      />
    </div>
  );
}
