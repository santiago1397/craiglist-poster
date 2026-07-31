// Every edit attempt against this posting, with the step trail behind it.
//
// The step breadcrumb is the point. Craigslist's edit form is DOM this codebase
// inferred rather than observed, so selector breakage is the expected failure —
// and "TimeoutError waiting for selector" without the page behind it is not
// something anyone can act on. Each attempt carries per-step timings, a
// `selectors` note listing what each selector matched, and links to the
// screenshot and HTML dump the desktop captured.

import { useState } from "react";
import { cn } from "../../lib/cn";
import { formatDateTime } from "../../lib/format";
import type { EditAttempt } from "../../lib/edits";

const ARTIFACT_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

const GOOD_OUTCOMES = ["applied", "no_change", "dry_run"];

export function PostEditHistory({ attempts }: { attempts: EditAttempt[] }) {
  const [open, setOpen] = useState(false);

  if (attempts.length === 0) return null;

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 text-left"
      >
        <h2 className="font-semibold">Edit history</h2>
        <span className="text-xs text-fg-muted">
          {attempts.length} attempt{attempts.length === 1 ? "" : "s"} · {open ? "hide" : "show"}
        </span>
      </button>

      {open && (
        <ul className="mt-3 space-y-3">
          {attempts.map((a) => {
            const good = GOOD_OUTCOMES.includes(a.outcome);
            return (
              <li key={a.event_id} className="rounded border border-border bg-bg/40 p-2">
                <div className="flex items-baseline justify-between gap-2 flex-wrap">
                  <span
                    className={cn(
                      "text-sm font-medium",
                      good ? "text-ok-fg" : "text-danger-fg",
                    )}
                  >
                    {a.outcome}
                  </span>
                  <span className="text-xs text-fg-subtle">
                    {formatDateTime(a.ts)}
                    {a.duration_seconds != null && ` · ${a.duration_seconds.toFixed(1)}s`}
                    {a.applied_rev != null && ` · applied rev ${a.applied_rev}`}
                  </span>
                </div>

                {a.error_message && (
                  <p className="mt-1 text-xs text-danger-fg font-mono whitespace-pre-wrap">
                    {a.failed_step ? `${a.failed_step}: ` : ""}
                    {a.error_message}
                  </p>
                )}

                {a.images_desired_count != null && (
                  <p className="mt-1 text-xs text-fg-muted">
                    images {a.images_live_count ?? "?"} live / {a.images_desired_count} desired
                  </p>
                )}

                {a.steps && a.steps.length > 0 && (
                  <ol className="mt-2 space-y-0.5 font-mono text-[11px]">
                    {a.steps.map((s, i) => (
                      <li
                        key={`${s.name}-${i}`}
                        className={s.ok ? "text-fg-muted" : "text-danger-fg"}
                      >
                        <span className="inline-block w-10">{s.ok ? "ok" : "FAIL"}</span>
                        <span className="inline-block min-w-40">{s.name}</span>
                        <span className="inline-block w-14 text-right tabular-nums">
                          {s.duration_seconds != null ? `${s.duration_seconds.toFixed(2)}s` : ""}
                        </span>
                        {s.note && (
                          <span
                            className={cn(
                              "ml-2 whitespace-pre-wrap break-all",
                              // The selector census lands here. It is the first
                              // thing worth reading when an edit fails, so it is
                              // not dimmed like an ordinary note.
                              s.name === "selectors" ? "text-fg" : "text-fg-subtle",
                            )}
                          >
                            {s.note}
                          </span>
                        )}
                      </li>
                    ))}
                  </ol>
                )}

                {a.artifact_ids && a.artifact_ids.length > 0 && (
                  <div className="mt-2 flex gap-2 flex-wrap">
                    {a.artifact_ids.map((id) => (
                      <a
                        key={id}
                        href={`${ARTIFACT_BASE}/artifacts/${id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs px-1.5 py-0.5 rounded border border-border-strong text-fg-muted hover:bg-surface-2"
                      >
                        artifact {id.slice(0, 8)}
                      </a>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
