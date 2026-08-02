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

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { cn } from "../../lib/cn";
import type { PostImageRef, PublishedImage } from "../../lib/edits";
import { ImageLightbox, type LightboxImage } from "./ImageLightbox";

const IMG_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

/** Sized for us: 480 for tiles, 1024 for the viewer, the original on request. */
function ours(id: number): Omit<LightboxImage, "key" | "slot" | "ours"> {
  return {
    thumbSrc: `${IMG_BASE}/images/${id}/thumb`,
    viewSrc: `${IMG_BASE}/images/${id}/thumb?w=1024`,
    fullSrc: `${IMG_BASE}/images/${id}/raw`,
  };
}

/**
 * Sized on Craigslist's side. Their CDN names the variant in the filename —
 * `..._1200x900.jpg`, `_600x450`, `_300x300` — so a tile can ask for a tile
 * rather than pulling a 1200px original and letting the browser shrink it.
 * An unrecognised name is left alone; a working picture beats a clever URL.
 */
const CL_VARIANT = /_(50x50c|300x300|600x450|1200x900)\.jpg$/i;
function craigslist(url: string): Omit<LightboxImage, "key" | "slot" | "ours"> {
  const full = url.startsWith("//") ? `https:${url}` : url;
  const at = (size: string) =>
    CL_VARIANT.test(full) ? full.replace(CL_VARIANT, `_${size}.jpg`) : full;
  return { thumbSrc: at("600x450"), viewSrc: at("1200x900"), fullSrc: at("1200x900") };
}

/** Our own copies first; a Craigslist URL only when we hold nothing better. */
function resolve(attached: PublishedImage[], manifest: PostImageRef[]): LightboxImage[] {
  if (attached.length > 0) {
    return attached.map((i) => ({
      key: `own-${i.id}`, slot: i.slot, ours: true, ...ours(i.id),
    }));
  }
  return manifest
    .filter((i) => i.image_id || i.url)
    .map((i) => ({
      key: `man-${i.slot}-${i.image_id ?? i.url}`,
      slot: i.slot,
      ours: !!i.image_id,
      ...(i.image_id ? ours(i.image_id) : craigslist(i.url as string)),
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
  const [viewing, setViewing] = useState<number | null>(null);
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
          {" Click any picture to see it full size."}
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
        {shown.map((img, i) => (
          <li key={img.key} className="relative">
            <button
              onClick={() => setViewing(i)}
              title={`${img.ours ? "Our copy" : "Served by Craigslist"} — click to open full size`}
              className="block rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-border"
            >
              <img
                src={img.thumbSrc}
                alt={img.slot === 1 ? "Cover image (Craigslist thumbnail)" : `Slot ${img.slot}`}
                loading="lazy"
                decoding="async"
                width={112}
                height={80}
                className={cn(
                  "h-20 w-28 object-cover rounded border bg-surface-2 transition",
                  "hover:brightness-110 hover:border-accent-border",
                  img.slot === 1 ? "border-accent-border" : "border-border-strong",
                )}
              />
            </button>
            <span className="absolute top-0.5 left-0.5 text-[10px] px-1 rounded bg-black/70 text-white pointer-events-none">
              {img.slot === 1 ? "cover" : img.slot}
            </span>
          </li>
        ))}
      </ul>

      <ImageLightbox
        images={shown}
        index={viewing}
        onIndexChange={setViewing}
        onClose={() => setViewing(null)}
      />
    </section>
  );
}
