// Prompt studio.
//
// Write a prompt, see what it produces, keep the good ones, set the one that
// automatic generation uses. Test renders are deliberately quarantined: they
// cost real money and produce real files, but nothing that feeds a live ad can
// see them until you press Keep.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";

type Prompt = {
  id: number;
  purpose: string;
  name: string;
  body: string;
  is_default: boolean;
  updated_at: string;
};

type TestImage = { id: number; bytes_size: number };

const PURPOSES = [
  { key: "cover_image", label: "Cover image", hint: "Slot 1 — the search thumbnail. Text is composited over its lower third, so keep that area clear." },
  { key: "photo_image", label: "Photo image", hint: "Slots 2 onward. Nothing is drawn over these, so they can be as detailed as you like." },
  { key: "ad_copy", label: "Ad copy", hint: "The system prompt that writes each listing's title and body." },
  { key: "keyword_tail", label: "Keyword tail", hint: "Appended verbatim to every ad. Not generated — this is the literal text." },
] as const;

type PurposeKey = (typeof PURPOSES)[number]["key"];

const IMG_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");
const isImage = (p: string) => p === "cover_image" || p === "photo_image";

export default function PromptsPage() {
  const [purpose, setPurpose] = useState<PurposeKey>("cover_image");
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [variables, setVariables] = useState<Record<string, string[]>>({});
  const [kinds, setKinds] = useState<string[]>([]);
  const [kindsDraft, setKindsDraft] = useState("");
  const [selected, setSelected] = useState<Prompt | null>(null);
  const [name, setName] = useState("");
  const [body, setBody] = useState("");
  const [tests, setTests] = useState<TestImage[]>([]);
  const [kept, setKept] = useState<Set<number>>(new Set());
  const [count, setCount] = useState(2);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await api.get<{
        prompts: Prompt[];
        variables: Record<string, string[]>;
        image_kinds: string[];
      }>("/prompts", { purpose });
      setPrompts(r.prompts);
      setVariables(r.variables);
      setKinds(r.image_kinds);
      setKindsDraft(r.image_kinds.join("\n"));
      const active = r.prompts.find((p) => p.is_default) ?? r.prompts[0] ?? null;
      setSelected(active);
      setName(active?.name ?? "");
      setBody(active?.body ?? "");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [purpose]);

  useEffect(() => {
    void load();
  }, [load]);

  // Anything not kept is paid-for junk; drop it when the operator navigates
  // away rather than letting it accumulate on the volume.
  useEffect(() => {
    return () => {
      void api.post("/prompts/test/discard").catch(() => {});
    };
  }, []);

  async function run(fn: () => Promise<unknown>, msg?: string) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await fn();
      if (msg) setNote(msg);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function pick(p: Prompt) {
    setSelected(p);
    setName(p.name);
    setBody(p.body);
  }

  async function test() {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const r = await api.post<{
        created: number; cost_usd: number; error: string | null; images: TestImage[];
      }>("/prompts/test", {
        body,
        kind: purpose === "cover_image" ? "cover" : "photo",
        count,
      });
      setTests(r.images ?? []);
      setKept(new Set());
      setNote(
        `${r.created} render${r.created === 1 ? "" : "s"} · $${r.cost_usd.toFixed(4)}` +
          (r.error ? ` · stopped: ${r.error}` : ""),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const dirty = !!selected && (name !== selected.name || body !== selected.body);
  const vars = variables[purpose] ?? [];
  const current = PURPOSES.find((p) => p.key === purpose)!;

  return (
    <div className="p-4 space-y-4">
      <header className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-lg font-semibold">Prompt studio</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            The prompt marked <strong>default</strong> is what automatic generation uses.
          </p>
        </div>
        <a href="/images" className="text-sm px-3 py-1 rounded border border-slate-700 text-slate-300 hover:bg-slate-800">
          ← Images
        </a>
      </header>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-200">{error}</div>
      )}
      {note && (
        <div className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300">{note}</div>
      )}

      <div className="flex gap-1 flex-wrap">
        {PURPOSES.map((p) => (
          <button
            key={p.key}
            onClick={() => { setPurpose(p.key); setTests([]); }}
            className={cn(
              "px-3 py-1.5 rounded text-sm",
              purpose === p.key ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-800/60",
            )}
          >
            {p.label}
          </button>
        ))}
      </div>
      <p className="text-xs text-slate-500 -mt-2">{current.hint}</p>

      <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
        <aside className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Saved ({prompts.length})</span>
            <button
              onClick={() => { setSelected(null); setName(""); setBody(""); }}
              className="text-xs px-2 py-0.5 rounded border border-slate-700 text-slate-300 hover:bg-slate-800"
            >
              + New
            </button>
          </div>
          <ul className="space-y-1">
            {prompts.map((p) => (
              <li key={p.id}>
                <button
                  onClick={() => pick(p)}
                  className={cn(
                    "w-full text-left px-2 py-1.5 rounded text-sm border",
                    selected?.id === p.id
                      ? "border-sky-600 bg-slate-800"
                      : "border-slate-800 hover:bg-slate-800/60",
                  )}
                >
                  <span className="block truncate">{p.name}</span>
                  {p.is_default && (
                    <span className="text-[10px] px-1 rounded bg-emerald-900/60 text-emerald-200">
                      default
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>

          {isImage(purpose) && (
            <div className="pt-3 border-t border-slate-800 space-y-1.5">
              <span className="text-xs text-slate-400">
                {"{kind}"} values ({kinds.length})
              </span>
              <p className="text-[11px] text-slate-600">
                One per line. Each image picks one at random — this is what stops
                every photo looking like the same house.
              </p>
              <textarea
                value={kindsDraft}
                onChange={(e) => setKindsDraft(e.target.value)}
                rows={7}
                className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-xs font-mono"
              />
              <button
                disabled={busy}
                onClick={() =>
                  run(
                    () => api.put("/prompts/image-kinds", { kinds: kindsDraft.split("\n") }),
                    "Kinds saved",
                  )
                }
                className="w-full text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-40"
              >
                Save kinds
              </button>
            </div>
          )}
        </aside>

        <section className="space-y-3">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Prompt name, e.g. “sunny suburban roof”"
            className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm"
          />
          {vars.length > 0 && (
            <p className="text-[11px] text-slate-500">
              Variables:{" "}
              {vars.map((v) => (
                <code key={v} className="mr-1.5 px-1 rounded bg-slate-800 text-slate-300">
                  {`{${v}}`}
                </code>
              ))}
            </p>
          )}
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={purpose === "keyword_tail" ? 18 : 10}
            placeholder="Write the prompt…"
            className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-sm font-mono"
          />

          <div className="flex gap-2 flex-wrap items-center">
            {selected ? (
              <>
                <button
                  disabled={busy || !dirty}
                  onClick={() => run(() => api.patch(`/prompts/${selected.id}`, { name, body }), "Saved")}
                  className="text-sm px-3 py-1 rounded bg-sky-700 hover:bg-sky-600 disabled:opacity-40"
                >
                  {dirty ? "Save changes" : "Saved"}
                </button>
                {!selected.is_default && (
                  <button
                    disabled={busy}
                    onClick={() => run(() => api.post(`/prompts/${selected.id}/default`), "Set as default")}
                    className="text-sm px-3 py-1 rounded bg-emerald-800 hover:bg-emerald-700 disabled:opacity-40"
                  >
                    Set as default
                  </button>
                )}
                <button
                  disabled={busy}
                  onClick={() => run(() => api.del(`/prompts/${selected.id}`), "Deleted")}
                  className="text-sm px-3 py-1 rounded border border-red-900 text-red-300 hover:bg-red-950/50 disabled:opacity-40"
                >
                  Delete
                </button>
              </>
            ) : (
              <button
                disabled={busy || !name.trim() || !body.trim()}
                onClick={() =>
                  run(() => api.post("/prompts", { purpose, name, body }), "Prompt created")
                }
                className="text-sm px-3 py-1 rounded bg-sky-700 hover:bg-sky-600 disabled:opacity-40"
              >
                Create prompt
              </button>
            )}

            {isImage(purpose) && (
              <>
                <span className="ml-auto text-xs text-slate-500">test renders</span>
                <input
                  type="number"
                  min={1}
                  max={8}
                  value={count}
                  onChange={(e) => setCount(Math.max(1, Math.min(8, Number(e.target.value) || 1)))}
                  className="w-14 bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                />
                <button
                  disabled={busy || !body.trim()}
                  onClick={() => void test()}
                  className="text-sm px-3 py-1 rounded bg-violet-700 hover:bg-violet-600 disabled:opacity-40"
                  title="Generate without touching the stack"
                >
                  {busy ? "Rendering…" : "Test"}
                </button>
              </>
            )}
          </div>

          {isImage(purpose) && tests.length > 0 && (
            <div className="rounded border border-violet-900/60 bg-violet-950/20 p-3">
              <p className="text-xs text-violet-200 mb-2">
                Test renders — not in your stack. Keep the ones worth having; the
                rest are deleted when you leave this page.
              </p>
              <ul className="grid gap-2 grid-cols-2 sm:grid-cols-4">
                {tests.map((t) => (
                  <li key={t.id} className="space-y-1">
                    <img
                      src={`${IMG_BASE}/images/${t.id}/raw`}
                      alt=""
                      className="w-full aspect-[4/3] object-cover rounded border border-slate-700"
                    />
                    <button
                      disabled={busy || kept.has(t.id)}
                      onClick={() =>
                        run(async () => {
                          await api.post(`/prompts/test/${t.id}/keep`);
                          setKept((k) => new Set(k).add(t.id));
                        })
                      }
                      className={cn(
                        "w-full text-xs px-2 py-1 rounded",
                        kept.has(t.id)
                          ? "bg-emerald-900/50 text-emerald-300"
                          : "bg-emerald-800 hover:bg-emerald-700",
                      )}
                    >
                      {kept.has(t.id) ? "Kept ✓" : "Keep"}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
