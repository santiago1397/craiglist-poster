# Post editing — design

How live Craigslist postings become editable from the dashboard, and how the
desktop reconciles them when it gets a chance.

Continues the decision numbering in [DESIGN.md](DESIGN.md) (which ends at 22).
Decisions below were settled in a design interview.

---

## Before / after

Today a post is write-once. `cl post` publishes it and the system never touches
it again — `stats.py` reads the postings page for counters, but nothing in the
codebase has ever opened a post's edit form. The VPS doesn't even store what a
post says: `posts` holds `post_id, account, title, url, posted_ts` and no body.

After: the dashboard owns a *desired state* per post. The desktop reconciles the
live posting to match it whenever it can take the browser lease.

```
DASHBOARD (VPS)                        DESKTOP (Windows)
  post_desired_state                     reporter-daemon
  post_edit_attempts (log)                 |- flush outbox    --> /events/batch
  image store (sha256)                     |- poll edit work  <-- /edits/pending  (15s)
  artifacts (screenshots/HTML)             |- hydrate  ------> PostContent event
  edit guardrails                          \- reconcile -----> PostEditAttempt event
        ^                                         |
        |                                    browser lease
        \------ hydrate/reconcile ---------->  (post > stats > edit)
```

---

## Decisions

### Content

23. **The desktop hydrates content from Craigslist.** The VPS stores no body
    today and `drafts` only covers queue-era posts — 24 postings predate the
    queue. The desktop opens a post's real edit form, scrapes it, and reports it
    up as a `PostContent` event. You edit what is actually live, for every post,
    with no silent drift from a stale draft.

24. **Hydration is on demand, not scheduled.** You click "load" on a post and it
    is hydrated on the next poll cycle. Piggybacking `stats-sync` would open
    every post's edit form every morning — a far stranger behavioural signature
    than reading a stats page, for posts you may never edit. Footprint stays
    proportional to actual use. The UI must show the pending state honestly.

25. **Desired state, not an edit queue.** One `post_desired_state` row per post
    holds the content you want, with `desired_rev` / `live_rev`. Editing twice
    before execution bumps a revision instead of queueing a second job. Re-running
    is idempotent, so a retry after a crash is always safe. Supersedes the
    obvious instinct to clone the `drafts` claim-and-consume queue — a draft is
    posted once, a post is edited repeatedly.

26. **Optimistic concurrency on apply.** Hydration stores a content hash. At
    apply time the desktop re-hydrates and compares; if the live post no longer
    matches what you were looking at when you edited, it parks rather than
    clobbers. Gone, expired, or ghosted posts park with a specific status. Same
    spirit as decision 16 — park, don't guess.

### Scheduling

27. **A machine-wide browser lease.** `launch_persistent_context` takes an
    exclusive Chrome lock on `profiles/<account>/`, and today `post`,
    `stats-sync` and `init-account` are kept apart *only* by non-overlapping
    Task Scheduler times. There is no lock anywhere. "Run edits on idle" makes
    collision inevitable, so all browser flows acquire a lease: `post` and
    `stats-sync` block on it, `edit` takes it opportunistically and skips if
    held. Stale leases are reclaimed by TTL so a crash doesn't wedge the box.
    Fixes an existing latent bug as a side effect.

28. **Edits defer to posting, always.** The edit worker refuses to start within
    N minutes of a posting slot. An edit is never worth delaying a post.

29. **The edit worker lives in the reporter-daemon**, which already runs
    continuously under an interactive logon with the machine token. A dedicated
    ~15s poll of `GET /edits/pending` keeps click-to-hydrating responsive; the
    existing 120s sync loop would put a two-minute spinner in front of a
    user-initiated action.

### Safety

30a. **Superseded 2026-08-01: the posting switch no longer gates editing.**
    Decision 30 made posting's kill switch the master switch, on the reading
    that pausing means "stop touching Craigslist". In use that was wrong: the
    two activities carry different risk, and pausing posting while fixing an ad
    is an ordinary thing to want that the coupling made impossible. It also let
    one switch silently override another — Settings showed editing enabled while
    nothing would ever edit. `edits_enabled` now stands alone; stopping
    everything means turning off both.

30. **Edits get the full guardrail treatment.** Server-owned settings in
    `guardrail_settings`, clamped to compiled ceilings in `config.py`, honouring
    the existing `posting_enabled` kill switch — the same shape as decisions 14
    and 15, so there is one mental model. New knobs: `edits_enabled`,
    `min_hours_between_edits_same_post`, `max_edits_per_account_per_day`,
    `max_edits_per_post_lifetime`, `edit_window_start/end_hour`. Without this,
    a bulk re-edit could put more anomalous activity on three accounts in an
    afternoon than the posting cadence allows in a fortnight.

31. **Failed attempts consume a guardrail slot.** A failure still burned a real
    browser session on that account. Charging for it is what stops a broken
    selector from retry-looping all day.

32. **Failure routing splits on what was mutated.** Decision 16 works because a
    failed post leaves nothing behind; a failed edit acts on something already
    live. Step classes:

    | Class | Steps | On failure |
    |---|---|---|
    | `PRE_MUTATION` | open, hydrate, diff, verify_hash | auto-retry, nothing touched |
    | `TEXT_ONLY` | fill title/body/fields (unsaved) | auto-retry, CL not committed |
    | `POST_IMAGE_REMOVAL` | images removed, not yet replaced | **re-upload in-session**, then park |
    | `POST_SAVE` | saved, outcome unknown | re-hydrate to determine, then park |

    The dangerous zone is unique to editing: a failure after removal leaves a
    live, earning post with zero images. Recovery is attempted immediately while
    the session is still open and the files are still cached locally. If that
    also fails the post parks as `degraded_live` with a loud alert.

### Images

33. **Full replace, verified.** Remove every image on the edit form, upload the
    desired set in order, assert the thumbnail count matches, abort and park if
    it doesn't. The only semantics that actually satisfies a declarative model,
    and the only one that gives deterministic slot-1 control — which the README
    calls the highest-leverage visual on the ad. Gated on the spike (below).

34. **Reuse the existing image stack; do not build a second one.** The design
    interview was conducted against a stale checkout and priced this decision as
    "build DESIGN.md decisions 12/13 from scratch". That was wrong: the image
    stack already shipped. `images` (migration 0005) is content-addressed by
    sha256 with `owner_account` bound on attach and `used_at` for the reuse
    cooldown — decision 13, already implemented. `app/storage.py` is the storage
    interface from decision 12, and MiniMax generation, the approval shelf and
    the Images page all exist.

    So editing adds only `post_desired_images`, deliberately mirroring
    `draft_images` (same slot semantics, same 1..5 constraint) so attaching an
    image behaves identically whether the target is a queued draft or a live
    posting. Ownership and cooldowns stay owned by `services/images.py`.

    One consequence worth stating: `images.detach` released an image's claim
    when no `draft_images` row held it. With a second holder that check would
    have released images still attached to a live posting, so it now considers
    both tables.

35. **Uploaded images bypass the cover pool.** You attach the exact image you
    want in slot 1, so `mark_cover_used()`, `covers/unclaimed/` and the one-shot
    claim model never enter the edit path. Removes the asset-accounting problem
    that re-uploading a cover would otherwise create.

### Observability

36. **Structured events plus artifacts.** A new `PostEditAttempt` event carries
    `outcome`, `failed_step`, a per-step breadcrumb with timings, `applied_rev`,
    and artifact references. Decision 17 finally gets built: screenshots and HTML
    dumps upload to the VPS (~2MB cap, 30-day retention) and render in the
    dashboard. Today `_dump_page()` writes them to `logs/failures/` on the
    Windows box, where they are useless from the VPS. A CL edit form is DOM this
    codebase has never touched, so selector breakage is the *expected* failure
    mode — an error string without the page behind it is not debuggable.

37. **Dry-run is read-only.** `cl edit --dry-run` opens the form, hydrates,
    diffs against desired, and reports the full plan and step timings to the VPS
    without typing a character. It deliberately does **not** mirror
    `cl post --dry-run`, which fills the form and uploads photos: CL commits
    image operations at selection time, so a fill-then-abandon rehearsal could
    strip a live post's images and walk away without applying the replacement —
    strictly more destructive than the real thing failing.

38. **A separate canary path proves the write.** `cl edit-canary <post_id>`
    performs one real end-to-end edit, refusing unless the post is on an explicit
    canary allowlist. Read-only rehearsal is safe on production any time; the
    canary is how you verify the save actually works.

---

## Derived requirements

- `posts` needs `body` and an image manifest, populated only by hydration —
  `post_attempt` must not write them, or a re-post would overwrite live truth.
- The lease must be taken by `stats-sync` and `post` **before** this feature
  ships, otherwise edits inherit a race that already exists. Implemented inside
  `launch_account` rather than at each call site, so a future flow cannot forget.
- `GET /edits/pending` returns both `hydrate` and `reconcile` work in one call,
  so the 15s poll costs one request, not two.
- Artifact upload needs its own size guard on the desktop side: a full-page
  screenshot of a long CL posting can exceed 2MB. Captured as JPEG down a
  quality ladder, falling back to viewport-only, so the cap is respected at
  capture time rather than rejected after upload.
- Artifacts spool to `logs/artifacts/` rather than riding the event outbox — a
  2MB blob inside a 100-event JSON batch would be hostile to it. The artifact id
  is minted at capture time so the event can reference it before the upload
  completes.
- Hydration must be timestamp-guarded. The outbox can deliver out of order after
  a retry, and an older read overwriting a newer one would move `content_hash`
  backwards and break decision 26's staleness check.
- The Edits UI needs a `degraded_live` tray distinct from parked — a post with
  zero images is an active emergency, not a queue item.

---

## Spike — gates everything

Run manually against one throwaway post before any code is written. Every
question below is currently unanswered and at least one design decision depends
on each:

1. **Can images be removed from a live post's edit form?** Decision 33 dies
   without it and the image half needs redesigning.
2. **Can images be reordered**, or is order purely upload sequence?
3. **Does editing re-trigger CL review** — can a healthy post be ghosted by an
   edit? If yes, decision 30's limits need to be much tighter.
4. **Does editing reset the post's age, expiry, or search ranking?**
5. **Does the edit form autosave**, or is everything committed on submit? Bears
   directly on decision 36.
6. **What does the edit form's DOM look like** — selectors for title, body,
   image controls, save.

Write the findings up before starting phase 1.

---

## Rollout

| Phase | Contents | Status |
|---|---|---|
| 0 | **Spike.** Manual, one throwaway post. Answers the six questions above. | **outstanding — human only** |
| 1 | Text editing end to end: lease, hydration, desired state, reconcile, edit guardrails, `PostEditAttempt`, artifact upload, dry-run, canary, Edits UI. | built |
| 2 | Image store + upload UI. | **already existed** (migration 0005, decision 34) |
| 3 | Full-replace image reconcile (decision 33). | built, selectors unverified |
| 4 | Polish: diff view on parked edits, `degraded_live` alerting. | partial — statuses surface in the UI |
| 5 | Editing folded into the post's own page; the Edits tab retired. Full draft-parity form and 24-slot image picker (migration 0015). | built |

Phase 2 turned out to be already done, so what shipped is phases 1, 3 and part
of 4, hanging off the existing image stack.

**Phase 0 has not been done, and editing is now on anyway.** Migration 0015
flips `edits_enabled` to TRUE at the operator's direction, to iterate against
production. `config.py`'s compiled default stays FALSE — it governs only when
the server says nothing, so a desktop that cannot reach the VPS still will not
edit on its own initiative. The selectors in `editor.SEL` are inferred, never
observed; every one of them is a guess until the spike replaces it.

What changed to make running without the spike survivable:

- A field the form cannot reach now **fails the attempt** at
  `failed_step="unsupported_field"`, before anything is typed, naming the
  selector that missed. It used to be skipped silently and reported `applied`.
  That step is deliberately absent from `PRE_MUTATION_STEPS`, so it parks
  instead of retrying a selector that will never match.
- `county` and `service_offered` are no longer editable at all. The form has no
  control for either, so they were guaranteed to hit the case above.
- Every attempt records a **selector census** — what each selector matched — on
  its step trail, visible in the post's Edit history without downloading an
  artifact. A count of 2 matters as much as a count of 0: the fill helpers all
  take `.first`.
- `CL_EDIT_TRACE=1` captures the form on success as well as failure.

Recommended order once you can log in again:

1. `uv sync` and `cl init-account` for all three accounts — the accounts have
   been logged out since 2026-07-01 and nothing here works without that.
2. Run the spike. Answer the six questions — above all, *can images be removed
   from a live post's edit form?* Decision 33 and the whole image half depend on
   it. Update `editor.SEL`.
3. `CL_EDIT_TRACE=1 cl edit --dry-run` against a real post. It types nothing, so
   this is safe even with wrong selectors, and the artifacts show you the real
   form. Iterate until every census entry for a field you intend to edit reads
   exactly 1, and until an unchanged post reports `no_change` — if it does not,
   `content_hash` disagrees with itself and every real edit will park as
   `failed_stale`.
4. `cl edit-canary <post_id>` on a disposable posting with `CL_CANARY_POSTS`
   set. Text first, with images unmanaged; only then images.
5. Only then let the daemon's edit worker run against real inventory.

Rollback needs no deploy: `UPDATE guardrail_settings SET edits_enabled = FALSE,
edits_paused_reason = '<why>'`. It takes effect on the desktop's next config
fetch, and the reason shows up verbatim under Diagnostics → Editing live posts.
