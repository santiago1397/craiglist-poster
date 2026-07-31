// The tray for an edit that stopped and needs a person.
//
// Renders nothing in the ordinary case. It appears when an edit parked rather
// than applied — and, most importantly, when a posting is `degraded_live`,
// which means a live ad is in a worse state than before we touched it. That is
// an emergency, not a queue item, so it is styled as one.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { cn } from "../../lib/cn";
import { formatDateTime } from "../../lib/format";
import { editStatusExplanation, isDegraded, isParked, type EditablePost } from "../../lib/edits";

export function PostRecovery(props: {
  post: EditablePost;
  onError: (message: string) => void;
}) {
  const p = props.post;
  const qc = useQueryClient();
  const degraded = isDegraded(p);
  const parked = isParked(p);

  const requeue = useMutation({
    mutationFn: () => api.post(`/edits/${p.post_id}/requeue`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["edits", p.post_id] }),
    onError: (e) => props.onError(e instanceof ApiError ? e.message : String(e)),
  });

  if (!degraded && !parked) return null;

  const explanation = editStatusExplanation(p.edit_status);

  return (
    <section
      className={cn(
        "rounded-lg border p-4 space-y-2",
        degraded ? "border-danger-border bg-danger/10" : "border-warn-border bg-warn/10",
      )}
    >
      <h2 className={cn("flex items-center gap-2 font-semibold",
        degraded ? "text-danger-fg" : "text-warn-fg")}>
        <AlertTriangle size={16} aria-hidden />
        {degraded ? "This live posting is degraded" : "The edit stopped and is waiting for you"}
      </h2>

      {explanation && <p className="text-sm text-fg-muted">{explanation}</p>}

      {p.failed_message && (
        <p className="text-xs font-mono text-fg-muted bg-bg/60 rounded p-2 whitespace-pre-wrap">
          {p.failed_step ? `${p.failed_step}: ` : ""}
          {p.failed_message}
        </p>
      )}

      <p className="text-xs text-fg-subtle">
        Last attempt {formatDateTime(p.last_attempt_at)}
      </p>

      <div className="flex items-center gap-2 flex-wrap">
        {p.url && (
          <a
            href={p.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs px-2 py-1 rounded border border-border-strong hover:bg-bg"
          >
            Open on Craigslist
          </a>
        )}
        <button
          disabled={requeue.isPending}
          onClick={() => requeue.mutate()}
          className="text-xs px-2 py-1 rounded border border-border-strong hover:bg-bg disabled:opacity-40"
        >
          {requeue.isPending ? "Requeueing…" : "Try this edit again"}
        </button>
      </div>
      <p className="text-xs text-fg-subtle">
        Requeue only once you have dealt with the cause — a parked edit that goes
        straight back to pending will hit the same wall and burn another attempt
        against the daily cap.
      </p>
    </section>
  );
}
