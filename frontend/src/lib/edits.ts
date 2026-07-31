// Shapes returned by the /edits endpoints, and the small amount of logic that
// reads them. Shared by the post detail page and the Diagnostics editing card.

export type EditStep = {
  name: string;
  ok: boolean;
  duration_seconds: number | null;
  note: string | null;
};

export type EditAttempt = {
  event_id: string;
  ts: string;
  outcome: string;
  duration_seconds: number | null;
  desired_rev: number | null;
  applied_rev: number | null;
  steps: EditStep[] | null;
  failed_step: string | null;
  error_type: string | null;
  error_message: string | null;
  images_live_count: number | null;
  images_desired_count: number | null;
  artifact_ids: string[] | null;
};

/** `GET /edits/{post_id}` — the live posting plus its desired state. */
export type EditablePost = {
  post_id: string;
  account: string;
  url: string | null;
  posted_ts: string | null;

  // Live content, written only by hydration.
  title: string | null;
  body: string | null;
  body_head: string | null;
  city: string | null;
  county: string | null;
  postal_code: string | null;
  license_number: string | null;
  phone_number: string | null;
  live_status: string | null;
  editable: boolean;
  hydrated_at: string | null;
  hydrate_requested_at: string | null;
  hydrate_error: string | null;

  // Desired state, absent until the first edit.
  edit_status: string | null;
  desired_rev: number | null;
  live_rev: number | null;
  desired_title: string | null;
  desired_body: string | null;
  desired_city: string | null;
  desired_postal_code: string | null;
  desired_license_number: string | null;
  desired_phone_number: string | null;
  image_set_managed: boolean | null;
  failed_step: string | null;
  failed_message: string | null;
  last_attempt_at: string | null;

  attempts: EditAttempt[];
};

export type EditHealth = {
  global_blocks: string[];
  pending: number;
  applying: number;
  degraded: number;
  parked: number;
  hydrating: number;
};

export const PARKED_STATUSES = ["parked_stale", "parked_gone", "failed"];

export const isParked = (p: { edit_status: string | null }) =>
  PARKED_STATUSES.includes(p.edit_status ?? "");

export const isDegraded = (p: { edit_status: string | null }) =>
  p.edit_status === "degraded_live";

export const hasPendingEdit = (p: EditablePost) =>
  p.desired_rev !== null && p.live_rev !== null && p.desired_rev > p.live_rev;

/**
 * What each parked state means and what to do about it. Kept as prose because
 * the status alone ("parked_stale") tells an operator nothing about whether
 * their ad is currently broken.
 */
export function editStatusExplanation(status: string | null): string | null {
  switch (status) {
    case "degraded_live":
      return (
        "This posting is live and in a worse state than before the edit — most " +
        "likely missing images. Open it on Craigslist and check it now; this is " +
        "not something to leave until later."
      );
    case "parked_stale":
      return (
        "The live posting changed after you edited it, so the change was not " +
        "applied — applying it would have overwritten something you never saw. " +
        "Load it again to see what it says now, then redo your edit."
      );
    case "parked_gone":
      return (
        "The posting is no longer listed on the account page, so there was " +
        "nothing to edit. It probably expired or was removed."
      );
    case "failed":
      return (
        "The edit failed partway through and will not retry on its own. Read " +
        "the reason below — if it names a selector, the Craigslist form has " +
        "changed and the desktop needs updating before this can work."
      );
    case "applying":
      return "The desktop has this posting open right now.";
    default:
      return null;
  }
}
