// Look at one picture properly, and step through the rest without closing.
//
// The thumbnails on a post are 112px wide, which is enough to recognise a photo
// and not enough to judge it — and a post can carry 24 of them, so "open it in
// a new tab" means 24 tabs. This is the full-size view.
//
// Sizing rule, same as the Craigslist preview modal: the image is capped to the
// viewport, so `?w=1024` covers it at better than 1:1 on any normal screen at a
// fraction of the bytes. The original averages ~770KB and the browser would
// only downscale it. "Open original" is a deliberate second click, for when you
// actually want the file rather than a look at it.

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink, X } from "lucide-react";
import { RawModal } from "../Modal";

export type LightboxImage = {
  key: string;
  slot: number;
  /** Tile-sized. Used for the filmstrip, which is 24 images wide. */
  thumbSrc: string;
  /** Rendered in the viewer — sized for display, not the original. */
  viewSrc: string;
  /** The bytes themselves, opened in a new tab on request. */
  fullSrc: string;
  ours: boolean;
};

export function ImageLightbox({
  images,
  index,
  onIndexChange,
  onClose,
  caption,
}: {
  images: LightboxImage[];
  /** null when closed. */
  index: number | null;
  onIndexChange: (i: number) => void;
  onClose: () => void;
  caption?: string;
}) {
  const open = index !== null;
  const current = open ? images[index] : null;

  // Arrow keys, because nobody reaches for a mouse to page through 24 photos.
  // Radix already owns Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight") onIndexChange((index! + 1) % images.length);
      if (e.key === "ArrowLeft") onIndexChange((index! - 1 + images.length) % images.length);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, index, images.length, onIndexChange]);

  // A fresh spinner per picture. Craigslist's CDN is not always quick, and a
  // frame that goes blank with no explanation reads as broken.
  const [loading, setLoading] = useState(true);
  useEffect(() => setLoading(true), [current?.key]);

  if (!current || index === null) return null;
  const many = images.length > 1;
  const step = (d: number) => onIndexChange((index + d + images.length) % images.length);

  return (
    <RawModal
      open
      onOpenChange={(o) => !o && onClose()}
      label={`Picture ${index + 1} of ${images.length}`}
      className="items-center"
    >
      <div className="w-full max-w-6xl flex flex-col gap-2">
        <div className="flex items-center justify-between gap-3 text-white text-sm">
          <span className="tabular-nums">
            {index + 1} of {images.length}
            <span className="text-white/60">
              {" · "}
              {current.slot === 1 ? "cover" : `slot ${current.slot}`}
              {current.ours ? "" : " · served by Craigslist"}
            </span>
          </span>
          <div className="flex items-center gap-1">
            <a
              href={current.fullSrc}
              target="_blank"
              rel="noopener noreferrer"
              className="px-2 py-1 rounded text-xs bg-white/10 hover:bg-white/20 flex items-center gap-1"
            >
              <ExternalLink size={13} aria-hidden="true" /> Open original
            </a>
            <button
              onClick={onClose}
              aria-label="Close viewer"
              className="rounded p-1.5 hover:bg-white/20 text-white"
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
        </div>

        <div className="relative flex items-center justify-center bg-black/40 rounded min-h-[40vh]">
          {loading && (
            <span className="absolute text-white/70 text-sm">Loading…</span>
          )}
          <img
            key={current.key}
            src={current.viewSrc}
            alt={caption || `Picture ${index + 1} of ${images.length}`}
            decoding="async"
            onLoad={() => setLoading(false)}
            onError={() => setLoading(false)}
            className="max-h-[80vh] w-auto max-w-full object-contain"
          />
          {many && (
            <>
              <button
                onClick={() => step(-1)}
                aria-label="Previous picture"
                className="absolute left-1 top-1/2 -translate-y-1/2 rounded-full p-2 bg-black/60 text-white hover:bg-black/80"
              >
                <ChevronLeft size={22} aria-hidden="true" />
              </button>
              <button
                onClick={() => step(1)}
                aria-label="Next picture"
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded-full p-2 bg-black/60 text-white hover:bg-black/80"
              >
                <ChevronRight size={22} aria-hidden="true" />
              </button>
            </>
          )}
        </div>

        {many && (
          <div className="flex gap-1 flex-wrap justify-center">
            {images.map((img, i) => (
              <button
                key={img.key}
                onClick={() => onIndexChange(i)}
                aria-label={`Show picture ${i + 1}`}
                aria-current={i === index}
              >
                <img
                  src={img.thumbSrc}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  className={
                    "h-10 w-14 object-cover border-2 " +
                    (i === index ? "border-white" : "border-transparent opacity-60")
                  }
                />
              </button>
            ))}
          </div>
        )}
      </div>
    </RawModal>
  );
}
