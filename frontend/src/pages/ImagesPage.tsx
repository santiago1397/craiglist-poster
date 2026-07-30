// The image stack.
//
// Two shelves: PENDING holds freshly generated images nobody has looked at yet,
// APPROVED is the pool drafts actually draw from. Nothing reaches Craigslist
// without passing through your eyes.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";

type Image = {
  id: number;
  sha256: string;
  mime: string;
  bytes_size: number;
  source: string;
  status: string;
  kind: string;
  owner_account: string | null;
  prompt: string | null;
  cost_usd: string | null;
  used_at: string | null;
  created_at: string;
};

type Stats = {
  by_status: { status: string; kind: string; n: number }[];
  spend_usd: number;
  available: Record<string, number>;
  storage: { files: number; bytes: number };
};

const TABS = [
  { key: "pending", label: "Pending review" },
  { key: "approved", label: "Stack" },
  { key: "rejected", label: "Rejected" },
] as const;

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";

function mb(bytes: number): string {
  return bytes > 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${Math.round(bytes / 1024)} KB`;
}

export default function ImagesPage() {
  const [images, setImages] = useState<Image[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("pending");
  const [count, setCount] = useState(4);
  // Cover vs photo matters: covers get text composited on and are drawn from a
  // separate prompt, so the distinction has to be made at upload and generate.
  const [kind, setKind] = useState<"photo" | "cover">("photo");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [list, s] = await Promise.all([
        api.get<{ images: Image[] }>("/images", { status: tab, limit: 120 }),
        api.get<Stats>("/images/stats"),
      ]);
      setImages(list.images);
      setStats(s);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [tab]);

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

  async function generate() {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const r = await api.post<{ created: number; cost_usd: number; error: string | null }>(
        "/images/generate",
        { count, kind },
      );
      // Generation reports provider trouble in the body rather than failing, so
      // surface both outcomes: what arrived, and what went wrong.
      setNote(
        `Generated ${r.created} image${r.created === 1 ? "" : "s"} · $${r.cost_usd.toFixed(4)}` +
          (r.error ? ` · stopped early: ${r.error}` : ""),
      );
      setTab("pending");
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError(null);
    let added = 0;
    const skipped: string[] = [];
    for (const f of Array.from(files)) {
      const form = new FormData();
      form.append("file", f);
      // kind was never being sent, so every upload became a photo and a cover
      // could not be uploaded at all.
      const res = await fetch(
        `${BASE.replace(/\/+$/, "")}/images/upload?kind=${encodeURIComponent(kind)}`,
        { method: "POST", credentials: "include", body: form },
      );
      if (res.ok) added++;
      else skipped.push(`${f.name} (${res.status === 409 ? "already in stack" : `HTTP ${res.status}`})`);
    }
    setNote(`Uploaded ${added} file${added === 1 ? "" : "s"}` +
      (skipped.length ? ` · skipped ${skipped.join(", ")}` : ""));
    if (fileRef.current) fileRef.current.value = "";
    setTab("approved");
    setBusy(false);
    await load();
  }

  const pendingCount = stats?.by_status
    .filter((r) => r.status === "pending")
    .reduce((a, r) => a + r.n, 0) ?? 0;

  return (
    <div className="p-4 space-y-4">
      <header className="flex items-center justify-between gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">Images</h1>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            type="number"
            min={1}
            max={20}
            value={count}
            onChange={(e) => setCount(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
            className="w-16 bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
          />
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as "photo" | "cover")}
            className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            title="Covers get the phone number composited on and use their own prompt"
          >
            <option value="photo">photo</option>
            <option value="cover">cover</option>
          </select>
          <button
            disabled={busy}
            onClick={() => void generate()}
            className="text-sm px-3 py-1 rounded bg-violet-700 hover:bg-violet-600 disabled:opacity-40"
          >
            {busy ? "Working…" : "Generate"}
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            accept="image/jpeg,image/png,image/webp"
            onChange={(e) => void upload(e.target.files)}
            className="hidden"
            id="upload-input"
          />
          <label
            htmlFor="upload-input"
            className="text-sm px-3 py-1 rounded bg-sky-700 hover:bg-sky-600 cursor-pointer"
          >
            Upload
          </label>
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
      {note && (
        <div className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300">
          {note}
        </div>
      )}

      {stats && (
        <section className="rounded border border-slate-800 bg-slate-900/50 p-3 text-sm">
          <div className="flex gap-5 flex-wrap text-slate-400">
            <span>
              Total spend <strong className="text-slate-200">${stats.spend_usd.toFixed(4)}</strong>
            </span>
            <span>
              Storage {stats.storage.files} files, {mb(stats.storage.bytes)}
            </span>
            {Object.entries(stats.available).map(([acct, n]) => (
              <span key={acct}>
                {acct} <strong className="text-slate-200">{n}</strong> available
              </span>
            ))}
          </div>
        </section>
      )}

      <div className="flex gap-1">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              "px-3 py-1.5 rounded text-sm",
              tab === t.key ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-800/60",
            )}
          >
            {t.label}
            {t.key === "pending" && pendingCount ? ` (${pendingCount})` : null}
          </button>
        ))}
      </div>

      {images.length === 0 ? (
        <p className="text-slate-500 text-sm py-10 text-center">
          {tab === "pending"
            ? "Nothing waiting for review. Generate some images to get started."
            : tab === "approved"
              ? "The stack is empty — drafts have no images to draw from."
              : "Nothing rejected."}
        </p>
      ) : (
        <ul className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-5">
          {images.map((img) => (
            <li
              key={img.id}
              className="rounded border border-slate-800 bg-slate-900/40 overflow-hidden"
            >
              <img
                src={`${BASE.replace(/\/+$/, "")}/images/${img.id}/raw`}
                alt={img.prompt ?? `image ${img.id}`}
                loading="lazy"
                className="w-full aspect-[4/3] object-cover bg-slate-950"
              />
              <div className="p-2 space-y-1.5">
                <div className="flex items-center gap-1 flex-wrap text-xs">
                  <span className="text-slate-500">#{img.id}</span>
                  {img.owner_account && (
                    <span className="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">
                      {img.owner_account}
                    </span>
                  )}
                  {img.source === "uploaded" && (
                    <span className="px-1.5 py-0.5 rounded bg-sky-900/60 text-sky-200">
                      uploaded
                    </span>
                  )}
                  {img.used_at && (
                    <span className="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">
                      published
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-slate-500">{mb(img.bytes_size)}</p>
                <div className="flex gap-1">
                  {img.status !== "approved" && (
                    <button
                      disabled={busy}
                      onClick={() => mutate(() => api.patch(`/images/${img.id}`, { status: "approved" }))}
                      className="flex-1 text-xs px-2 py-1 rounded bg-emerald-800 hover:bg-emerald-700 disabled:opacity-40"
                    >
                      Approve
                    </button>
                  )}
                  {img.status === "pending" && (
                    <button
                      disabled={busy}
                      onClick={() => mutate(() => api.patch(`/images/${img.id}`, { status: "rejected" }))}
                      className="flex-1 text-xs px-2 py-1 rounded border border-slate-700 text-slate-300 hover:bg-slate-800 disabled:opacity-40"
                    >
                      Reject
                    </button>
                  )}
                  {img.status !== "pending" && (
                    <button
                      disabled={busy}
                      onClick={() => mutate(() => api.del(`/images/${img.id}`))}
                      className="flex-1 text-xs px-2 py-1 rounded border border-red-900 text-red-300 hover:bg-red-950/50 disabled:opacity-40"
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
