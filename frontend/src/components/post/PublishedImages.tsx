// The images a posting actually went out with.
//
// Two records can answer that, and they are not equally durable.
//
// The good one is the draft's own attachment rows: our bytes, content-addressed,
// kept indefinitely (`images.delete_image` refuses to remove anything already
// published). Only postings the queue produced have it.
//
// The weak one is `posts.images` — a manifest of Craigslist's own CDN URLs,
// captured by hydration or scraped back off an ended posting. It renders today
// and stops resolving when Craigslist prunes the ad, which is precisely when
// somebody wants to see what was on it. Archive turns the weak record into the
// good one by fetching those URLs onto the VPS.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { cn } from "../../lib/cn";
import type { PostImageRef, PublishedImage } from "../../lib/edits";

const IMG_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

type Shown = { key: string; slot: number; src: string; ours: boolean };

/** Our own copies first; a Craigslist URL only when we hold nothing better. */
function resolve(attached: PublishedImage[], manifest: PostImageRef[]): Shown[] {
  if (attached.length > 0) {
    return attached.map((i) => ({
      key: `own-${i.id}`,
      slot: i.slot,
      src: `${IMG_BASE}/images/${i.id}/thumb`,
      ours: true,
    }));
  }
  return manifest
    .filter((i) => i.image_id || i.url)
    .map((i) => ({
      key: `man-${i.slot}-${i.image_id ?? i.url}`,
      slot: i.slot,
      src: i.image_id ? `${IMG_BASE}/images/${i.image_id}/thumb` : (i.url as string),
      ours: !!i.image_id,
    }));
}

export function PublishedImages({
  postId,
  images,
  manifest = [],
  onError,
}: {
  postId: string;
  images: PublishedImage[];
  manifest?: PostImageRef[];
  onError?: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const archive = useMutation({
    mutationFn: () => api.post<{ stored: number; failed: number }>(
      `/posts/${postId}/archive-images`, {},
    ),
    onSuccess: (r) => {
      if (r.failed > 0 && r.stored === 0) {
        onError?.(`Could not fetch any of these images — Craigslist may have already pruned them.`);
      }
      qc.invalidateQueries({ queryKey: ["edits", postId] });
    },
    onError: (e) => onError?.(e instanceof Error ? e.message : String(e)),
  });

  const shown = resolve(images, manifest);
  if (shown.length === 0) return null;
  const cover = shown.find((i) => i.slot === 1);
  const borrowed = shown.filter((i) => !i.ours).length;

  return (
    <section className="rounded-lg border border-border bg-surface p-4 space-y-2">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <h2 className="font-semibold">Pictures this ad went out with</h2>
        <span className="text-xs text-fg-muted">
          {shown.length} image{shown.length === 1 ? "" : "s"}
          {cover ? " · slot 1 is the Craigslist thumbnail" : ""}
        </span>
      </div>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-subtle max-w-prose">
          {borrowed === 0
            ? "Our own copies, kept whether or not the posting is still up."
            : `${borrowed} of these are still coming from Craigslist and will stop ` +
              "loading once it prunes the posting."}
        </p>
        {borrowed > 0 && (
          <button
            onClick={() => archive.mutate()}
            disabled={archive.isPending}
            className="text-xs px-2 py-1 rounded border border-border-strong hover:bg-surface-2 disabled:opacity-50 whitespace-nowrap"
          >
            {archive.isPending ? "Fetching…" : "Keep copies here"}
          </button>
        )}
      </div>
      <ul className="flex gap-2 flex-wrap">
        {shown.map((img) => (
          <li key={img.key} className="relative">
            <img
              src={img.src}
              alt={img.slot === 1 ? "Cover image (Craigslist thumbnail)" : `Slot ${img.slot}`}
              loading="lazy"
              decoding="async"
              width={112}
              height={80}
              className={cn(
                "h-20 w-28 object-cover rounded border bg-surface-2",
                img.slot === 1 ? "border-accent-border" : "border-border-strong",
                !img.ours && "opacity-90",
              )}
              title={img.ours ? "Our copy" : "Served by Craigslist"}
            />
            <span className="absolute top-0.5 left-0.5 text-[10px] px-1 rounded bg-black/70 text-white">
              {img.slot === 1 ? "cover" : img.slot}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
