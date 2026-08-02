// The images a posting actually went out with.
//
// Worth being clear about which record this is. `posts.images` is a manifest of
// Craigslist's own CDN URLs, captured by hydration — and those stop resolving
// when the posting ends, which is precisely when somebody wants to see what was
// on it. These are our own bytes: content-addressed, kept indefinitely
// (`images.delete_image` refuses to remove anything already published), and
// reachable from the draft that produced the post.
//
// Only postings the queue produced have this. Anything posted before the image
// stack existed has no record of its pictures anywhere.

import { cn } from "../../lib/cn";
import { formatDate } from "../../lib/format";
import type { PublishedImage } from "../../lib/edits";

const IMG_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

export function PublishedImages({ images }: { images: PublishedImage[] }) {
  if (images.length === 0) return null;
  const cover = images.find((i) => i.slot === 1);

  return (
    <section className="rounded-lg border border-border bg-surface p-4 space-y-2">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <h2 className="font-semibold">Pictures this ad went out with</h2>
        <span className="text-xs text-fg-muted">
          {images.length} image{images.length === 1 ? "" : "s"}
          {cover ? " · slot 1 is the Craigslist thumbnail" : " · no cover"}
        </span>
      </div>
      <p className="text-xs text-fg-subtle">
        Our own copies, kept whether or not the posting is still up. Craigslist's
        versions stop loading once an ad ends.
      </p>
      <ul className="flex gap-2 flex-wrap">
        {images.map((img) => (
          <li key={img.id} className="relative">
            <img
              src={`${IMG_BASE}/images/${img.id}/thumb`}
              alt={img.slot === 1 ? "Cover image (Craigslist thumbnail)" : `Slot ${img.slot}`}
              loading="lazy"
              decoding="async"
              width={112}
              height={80}
              className={cn(
                "h-20 w-28 object-cover rounded border bg-surface-2",
                img.slot === 1 ? "border-accent-border" : "border-border-strong",
              )}
              title={img.used_at ? `Published ${formatDate(img.used_at)}` : undefined}
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
