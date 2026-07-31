# Post management — design

How the system changes from "the desktop invents an ad at post time" to "the VPS
owns a reviewable queue and the desktop executes it".

Decisions below were settled in a design interview. Where a later decision
supersedes an earlier one, that is called out explicitly.

---

## Before / after

Today a post does not exist until the moment it is published. `generate_ad()`
fires on the Windows box and invents everything on the spot: random Excel row,
random spintax branch, random photo count, random cover claim. There is no
"upcoming post" to preview, reorder or edit.

After: the VPS owns a queue of concrete drafts. The desktop asks what to post
and reports what happened.

```
DASHBOARD (VPS)                      DESKTOP (Windows)
  drafts + queue                       reporter-daemon
  eligibility + throttles                |- flush outbox   --> /events/batch
  prompt library + generation            |- prefetch top-N <-- /queue
  image store (sha256)                   \- heartbeat
  error log + artifacts
        ^                              cl post @ 9/1/5
        \--------- claim -------------->  POST /queue/claim
                                          -> draft + image list
                                          -> patchright publishes
                                          -> post_attempt event
```

---

## Decisions

### Content and queue

1. **Queue-only, fail-closed.** `generate_ad()` retires. The desktop posts only
   pre-composed drafts pulled from the server. An empty queue means it posts
   nothing and the dashboard raises an alert. Excel becomes a bulk-import path
   *into* the queue.

2. **Ordered per-account queue**, plus optional `not_before` and `expires_at`
   windows per draft. You control order and target account; the guardrails
   control wall-clock time. The dashboard renders a projected calendar.

3. **Claim across all eligible accounts.** At the slot, the server returns
   head-of-queue from whichever eligible account has idled longest *and* has
   drafts, so one dry queue never wastes a posting slot.

4. **Prefetch early, claim late.** The sync thread mirrors the top ~10 drafts
   and their image bytes into a local cache but commits to nothing. The draft is
   chosen atomically at the moment the slot fires, so reordering wins until the
   last second.

5. **Nightly top-up.** A job refills the queue to a target depth when it drops
   below a floor. New drafts are auto-queued marked `UNREVIEWED`.

6. **Similarity is advisory only.** Trigram similarity (`pg_trgm`) is computed
   and displayed but never blocks. Scored on the *head* text only — the shared
   keyword tail would peg every comparison near 0.98.

### Copy generation

7. **MiniMax-Text-01 writes title + head**; the ~6,000-word keyword / zip / city
   tail is appended from a stored template. Generating the tail per draft would
   cost ~100x the tokens and risks corrupting a block that must stay verbatim.

8. **The prompt is editable in the dashboard.** Every draft remains manually
   editable before and after queueing.

9. **The 100-row workbook becomes seed briefs** (city, county, zip, service,
   phone, license, angle).

### Images

10. ~~Images are generated per draft.~~ **Superseded by decision 20.**

11. **MiniMax makes the picture, Pillow composites the text** from reusable
    overlay templates. Diffusion models render text unreliably; Pillow is
    pixel-perfect and re-rendering an overlay costs nothing. Uploaded covers
    pass through untouched.

12. **VPS volume behind authenticated routes**, content-addressed by sha256,
    behind a storage interface so S3/R2 is a one-file swap.

13. **Shared deck, claim on attach.** Uploads land with no owner. Attaching an
    image to a draft binds it permanently to that draft's account; detaching
    releases it only if it never posted. Per-account depth means "available to
    this account". Generated images are unique by construction, so this matters
    mostly for uploads.

### Control and safety

14. **Server-owned throttles with compiled-in ceilings.** Caps become editable
    settings; the desktop pulls them and clamps to maximums that live in
    `config.py`. A mistyped `30/day` applies as 5 and logs a warning.

15. **The server is authoritative** for post history and eligibility.
    `pick_next_account()` and the eligibility math move server-side.
    `state.json` demotes to a local audit trail.

16. **Failure path splits on how far the post got.** Failures before any photo
    upload touched no assets: the draft auto-returns to the head of the queue.
    Failures after uploads began have burned assets: the draft parks in a
    needs-attention tray with consumed images flagged.

17. **Every flow reports errors to the VPS** through the existing durable
    outbox, with screenshots and HTML dumps uploaded and viewable in the
    dashboard. ~2MB cap per artifact, 30-day retention.

### Later additions

18. **Image generation sits behind a provider interface**
    (`generate(prompt, aspect, n) -> bytes[]`). MiniMax is the first adapter;
    switching to Gemini is one file.

19. **Separate revocable per-machine token**, distinct from
    `INGEST_BEARER_TOKEN`, so a compromised desktop can be cut off without
    breaking ingest.

20. **Stack-first image sourcing.** Drafts draw only from the approved stack.
    A background job refills a `PENDING` shelf when depth drops below a floor;
    you approve keepers into the stack and discard the rest. Supersedes 10.

21. **Versioned prompt library**, three purposes (ad copy / cover image / photo
    image), one `ACTIVE` version each. Test runs never touch the queue or stack.
    Every draft and image records the prompt version that produced it, so
    impressions can be compared across prompt versions.

22. **No cooling period.** Anything queued is claimable immediately, including
    drafts you have not read. Review is a triage surface, not a gate.

### Two image stacks

Settled in a second design interview. These supersede parts of 10-13.

23. **Cover and photo are a hard partition, not a label.** Slot 1 accepts only
    `kind='cover'`, slots 2-24 only `kind='photo'`, and `pick_for_draft` filters
    on kind. Previously `kind` chose the generation prompt and then stopped
    mattering, so a cover — phone number composited across it — could be drawn
    into slot 4 as an ordinary photo. `set_kind` moves an image between stacks,
    which is what makes the partition liveable.

24. **Covers are chosen by hand; photos are not.** Generation attaches photos
    only and leaves slot 1 empty. It used to claim a cover per draft, which
    spent a curated stack across 45 queued drafts weeks before any published,
    and made every "manual" choice really an edit of a machine's choice.

25. **The cover has a claim-time backstop.** An empty slot 1 does not publish a
    coverless ad — the desktop uploads in slot order and Craigslist thumbnails
    whatever lands first — so `claim_next` attaches a cover in the moment it
    hands the draft out, and files a `flow_error` saying it did. A draft with no
    images at all is left alone: that is the imageless roll, not an oversight.

26. **Assignment is a reservation.** An image attached to a live draft, or
    staged on a live posting's desired set, is excluded from selection.
    `draft_images` has no uniqueness on `image_id` and nothing checked, so
    top-up handed the same photo to several queued drafts routinely. The picker
    still offers reserved images, greyed and behind a confirm, because
    deliberate reuse is legitimate — accidental reuse is what gets posts
    ghosted.

27. **Five buckets, derived not stored.** pending → available → assigned →
    published → rejected, per kind, mutually exclusive because `attach`
    requires `approved`. A bucket column would be a second source of truth able
    to disagree with the attachment table.

28. **Posts carry 23 photos.** `photos_min`/`photos_max` move to 23/23 rather
    than becoming a constant, so the count stays dialable and `imageless_rate`
    keeps working — roughly a tenth of drafts still take nothing at all, and
    those need no cover either. That roll is the only variation left in the
    image profile, which is the cost of uniform 24-image posts, accepted
    knowingly.

29. **A post-upload failure retires the images the site saw.** `mark_used` ran
    only from `mark_posted`, so a run that uploaded four photos and died at
    `publish` left all four looking fresh. Ingest now marks the first
    `photos_confirmed` attached images used; a missing count retires all of
    them, because retiring a clean image costs $0.0035 and re-showing a burned
    one costs an account.

30. **Short stacks are a state, not an error.** Drafts take what exists and
    publish thinner. The shortfall is stated continuously on the Images page and
    in Diagnostics rather than raised once, because with manual refill it is the
    ordinary condition.

31. **Automatic stack refill ships disabled.** 24-image posts need ~1,035
    standing photos and ~69/day; only a background generator sustains that. It
    exists behind `image_topup_enabled`, defaulting false, because it spends
    money on prompts still being tuned. Same pattern as `edits_enabled`.

### Posting on demand

32. **"Post now" changes the timing, never the permission.** Posting was
    time-triggered only: Task Scheduler at 9/13/17, server picks the draft. The
    operator could not test a copy or image change without waiting for a slot.
    The button sets a flag; the daemon's 15s poll spawns the ordinary `cl post`
    with a draft id. Every guardrail still runs server-side at claim time, and a
    manual post consumes one of the day's three slots like any other.

33. **The request is refused synchronously, not queued.** A click that cannot be
    honoured now says so now, with the reasons `cl status` would print. A
    request that lingered until the guardrails happened to clear would publish
    unattended at a time nobody chose — the opposite of what an on-demand button
    is for. This is what removes the need for pending-state UI: there is a
    20-minute TTL, but only as a backstop for the daemon being down, and it
    writes its reason onto the draft so the dashboard can answer "I pressed it
    and nothing happened".

34. **The flag is the authorisation, not the draft id.** `claim_next(draft_id=)`
    refuses unless that draft carries a live request. Without it, naming an id
    would let any machine token pull any draft out of order — the id says
    *which*, the flag says *may*. A targeted claim returns the requested draft
    or nothing; it never falls through to the rotation, because the operator
    clicked one row and a different ad publishing is worse than none.

35. **An in-flight claim blocks its account.** Every guardrail counts rows in
    `posts`, which ingest fills only once the attempt is reported — so during a
    run the history is stale and a second claim is authorised against it. The
    browser lease serialises the two Chromes but cancels neither, so both
    publish. This predates "Post now" (a 12:59 fire and a 13:00 fire hit it),
    but the button made it one click away. The claim itself is the only evidence
    of a run in progress, so eligibility now reads it. Bounded by the existing
    45-minute stale-claim reaper.

36. **The daemon spawns `cl post` rather than posting in-thread.** That path —
    machine binding, outbox flush, image fetch, the form, the failure taxonomy —
    is the most carefully tested code here, and a second implementation would
    have to be kept correct forever. The cost is one subprocess and one subtle
    interaction: `cl post` decides whether it may claim by sampling the outbox,
    and `flush_once` selects, POSTs and marks sent as three statements, so a
    daemon flush in flight reads as a phantom backlog and gets the child's claim
    refused. The daemon holds its flusher for the duration
    (`reporter.pause_flushing`).

---

## Derived requirements

- `PostAttempt` carries a structured **`failed_step`**. `poster.py` already
  tracks the step locally for failure dumps but never ships it; decision 16
  depends on it.
- The desktop **flushes its outbox before claiming**. A backlog of unsent
  `post_attempt` events would leave the server's history stale and let it
  authorise a post that breaches a cooldown. Only attempts with
  `outcome='posted'` hold up a claim: skips, dry runs and failures carry no
  history, and blocking on them strands the queue behind noise — permanently if
  the reporter is unconfigured, which is exactly the state the production
  machine was found in (11 unsent `skipped_no_eligible` events, no
  `REPORTER_URL`).
- The calendar projection is computed server-side (decision 15 supersedes the
  original plan of projecting from `next_eligible_at` shipped on the heartbeat).
- Generated images use Craigslist's thumbnail aspect (~4:3), not square — slot 1
  is the highest-leverage visual and a square gets cropped.

---

## Modules

**Review** — one screen with filters:
- `UNREVIEWED` drafts: edit title/body, swap or remove images, reorder, delete,
  mark reviewed
- `NEEDS ATTENTION`: drafts that failed after photo upload, consumed assets
  flagged
- Queue health: draft depth and stack depth per account, similarity flags
- Projected calendar

**Studio** — prompt bench and image factory:
- Prompt library with edit history, one `ACTIVE` per purpose
- Test runs against a seed brief, output side-by-side with prior versions
- Image generation into a `PENDING` shelf, approve into the stack
- Pillow overlay templates previewed on real stack images
- Prompt-version provenance joined against impressions/views

---

## Rollout

| Phase | Contents |
|---|---|
| 1 | drafts + queue + claim + server eligibility + error events. Text-only posts. |
| 2 | MiniMax copy, prompt library and test bench |
| 3 | image storage, generation provider, stack + pending shelf |
| 4 | Pillow cover overlays |
| 5 | uploads, calendar polish |

Phase 1 is a thin vertical slice that proves the riskiest parts — server owns
eligibility, desktop claims late, errors report end to end — before any image
pipeline exists. Posts with no images are already valid; ~10% are meant to go
out that way.

**Prerequisite, not part of this design:** all three accounts have been logged
out since 2026-07-01, and the local venv points at a `uv` Python install with no
`python.exe`. Running phase 1 needs `uv sync` and three `cl init-account` logins.
