# Diagnostics — how to find out what went wrong

Start at **Diagnostics** in the dashboard. Everything below explains what you
are looking at and what to do about it.

The system is two machines that only talk through a durable queue: a Windows
desktop that drives Chrome, and a VPS that owns the queue and the truth. That
design is why a failure is never obvious from one side alone — the desktop knows
*what* broke, the VPS knows *whether it mattered*, and this page is where the
two meet.

---

## The one rule

**Every failure reports itself, and reporting survives an outage.**

Everything the desktop learns goes into a local SQLite outbox and is delivered
to the VPS with retries. A network blip, a VPS reboot, or a machine that is
switched off for the weekend delays reporting — it does not lose it. So if
something is missing from Diagnostics, the desktop genuinely never got that far.

That is also why one thing is *not* covered by the rule: if the desktop never
runs at all, it cannot report that it never ran. See
[Machine offline](#machine-offline) — that check runs on the VPS instead.

---

## The four kinds of problem

| Kind | Means | Source |
|---|---|---|
| **Posting run** | A `cl post` attempt failed | `post_attempts` where outcome starts with `failed` |
| **Background job** | Any other flow raised — queue sync, stats, ghost check, edits | `flow_errors` |
| **Stuck draft** | A machine claimed a draft and never reported back | `drafts` still in `claimed` |
| **Machine offline** | A desktop has stopped calling home entirely | absence of `account_states` heartbeats |

### Severity means consequence, not volume

- **Critical** — posting is stopped, or a live ad is wrong right now.
- **Warning** — something failed but the system routed around it.
- **Info** — worth knowing, nothing is broken.

A failed stats scrape is a warning: no ad is affected. An unreachable queue is
critical: posting is fail-closed, so *nothing* goes out until it clears.

---

## Reading a posting failure

Posting walks a fixed sequence of named steps. The step it died on is the single
most useful fact, because the server routes the draft on it:

```
build_ad → launch → warmup → login_check → open_post_form → dismiss_reuse_prompt
   → advance_to_type → type_service_offered → category_skilled_trade
   → advance_to_form → form_title → form_zip → form_city → form_license
   → form_phone → form_body → form_validation → map_confirm → map_validation
   ─────────────────── nothing consumed above this line ───────────────────
   → photo_upload → preview → publish → billing → confirmation
```

The two `*_validation` steps look like padding and are not. Craigslist answers a
rejected form and an unadvanced map with HTTP 200 and the same page, so a run
that went nowhere keeps walking and dies at whatever selector it looks for next
— which is `photo_upload`, the first step *below* the line. Both failures
therefore used to arrive as `Timeout 30000ms exceeded waiting for
input[type='file']`: a message naming the uploader, which was never involved,
while the draft parked and its images burned for a run that showed Craigslist
nothing. Each check exists to fail early enough to stay above the line.

`map_validation` covers both directions of the map step. Craigslist asks which
region a posting belongs to when the ZIP geocodes outside the one the account
posts from (33410 resolves to Treasure Coast; the accounts post South Florida),
and it hides the continue button until that is answered — so the run stalls on
the map. Answering it *is* the map submission, so continuing again afterwards
overshoots onto the preview page instead. Stuck on the map and one page past it
both mean "no uploader here", and `map_validation` names which.

**Above the line**, no image ever reached Craigslist, so the draft goes straight
back to the head of the queue. Nothing was lost; the next slot retries it.

**Below the line**, images were uploaded and are burned. The draft parks in
**Review → Needs attention** and waits for you, because silently re-uploading
images to Craigslist is how accounts get flagged.

> The desktop must never report an unknown step. An unrecognised step is treated
> as below the line — parking is recoverable, re-uploading is not — so a launch
> failure that consumed nothing would otherwise cost you a manual rescue.
> `tests/test_failure_reporting.py` enforces this.

### The artifacts are the point

Every posting failure now uploads a **screenshot** and the **page HTML** to the
VPS, linked from the problem row. Craigslist changes its selectors periodically,
and "TimeoutError waiting for selector" is indistinguishable from an outage
until you see the page it actually served. Open the HTML dump and search for the
field name — that is usually a two-minute fix rather than an afternoon.

Artifacts expire after 30 days and are capped at 2MB each.

---

## Published, but degraded

A post can succeed and still be wrong. These are the cases:

| Warning | What it costs you |
|---|---|
| A photo never rendered a thumbnail | The ad is live with fewer images than intended |
| Thumbnail count mismatch | Craigslist rejected an upload, or the selector missed it |
| Cover kept claimed | The ad's thumbnail is not the edited cover you meant to use |
| Subarea: no county match | **The ad is filed under a county nobody chose** |
| No `/d/` link and no PostingID | The post URL saved is session-bound and will 404 later |
| Navigation exhausted its steps | Craigslist's page flow changed under us |

These deliberately keep `outcome = 'posted'`. The ad published, so it must still
count against the 24-hour and weekly caps — flipping the outcome would corrupt
the history that eligibility is computed from, and could authorise a post that
breaches a cooldown.

Instead they are mirrored into the problem feed as **critical**, because a live
ad being wrong is worse than a job that failed cleanly.

---

## Stuck drafts

A claim is a promise to report an outcome. When the desktop dies between the two
— Task Scheduler hitting its 30-minute execution limit mid-upload, a reboot, a
crash — nothing moved the draft on.

This used to be silent and permanent. Queue depth counts only `queued` drafts,
so a stranded `claimed` draft made the account report **"queue empty: no
drafts"** and quietly stop posting, while nightly top-up cheerfully refilled
around it.

Now a claim older than 45 minutes is swept at the next claim attempt and parked
in **Needs attention**.

**They are parked, not requeued, and that is deliberate.** We do not know what
the dead run did. If it published and its event is still sitting in the
desktop's outbox, requeueing would post the same ad twice — and that event
*will* arrive eventually, because the outbox is durable. Parking costs one
click; a duplicate live ad costs an account.

> **Before requeueing a stuck draft, check the account on Craigslist.** If the
> ad is already there, delete the draft instead.

The good case self-heals: when the delayed event finally arrives, it moves the
draft straight to `posted` with no human involved.

---

## Machine offline

Every other signal here needs the desktop to report something. When the desktop
is off, the Scheduled Task is disabled, or the reporter daemon has died, there
is no error to report — just an ageing "last post" and silence.

The reporter daemon heartbeats every 5 minutes. After **20 minutes** with no
heartbeat, the VPS raises this itself.

To fix, in order:
1. Is the machine on and **logged in**? The browser needs a desktop session.
2. Is the reporter daemon running? (`scripts/install-reporter-daemon.ps1`)
3. Can it reach the VPS? Check `QUEUE_URL` and `MACHINE_TOKEN` on the desktop.
4. Is the token still valid? **Settings → Machine tokens**.

---

## Checking from the desktop

```bash
uv run cl status      # eligibility, outbox depth, browser lease holder, stats health
uv run cl outbox      # pending vs sent events, retry counts
uv run cl tail        # live tail of logs/run.log
```

`cl status` answers "why is nothing posting" from the machine's own point of
view, including whether its outbox is backed up — which blocks claiming on
purpose, so the server never authorises a post while its history is stale.

Local artifacts are kept in `logs/failures/` even after upload, so they are
still there when the machine has no network.

---

## API

All cookie-authenticated (operator surface, not machine).

| Route | Returns |
|---|---|
| `GET /diagnostics` | The problem feed. `hours`, `include_acknowledged`, `limit` |
| `GET /diagnostics/summary` | Counts only — cheap enough to poll |
| `POST /diagnostics/acknowledge` | Mark flow errors as seen |
| `GET /diagnostics/posts/{post_id}/attempts` | Every run that produced this post |
| `GET /diagnostics/drafts/{draft_id}/attempts` | Every run against this draft |
| `GET /artifacts/{id}` | Screenshot or page HTML |

Acknowledging is not fixing. Stuck claims and silent machines ignore it by
design — they clear when the underlying condition clears, and letting you
dismiss them would hide something still true.

---

## Where the code lives

| Concern | File |
|---|---|
| Problem feed, severity, explanations | `backend/app/services/diagnostics.py` |
| Stale-claim sweep | `backend/app/services/queue.py` — `release_stale_claims` |
| Draft routing on failure | `backend/app/services/queue.py` — `release_or_park`, `PRE_UPLOAD_STEPS` |
| Event → table dispatch | `backend/app/services/ingest.py` |
| Step tracking, degradations, captures | `src/craigslist_auto/poster.py` — `PostRun` |
| Event reporting, outbox | `src/craigslist_auto/reporter.py` |
| Artifact capture and upload | `src/craigslist_auto/artifacts.py` |
| Regression tests | `tests/test_failure_reporting.py`, `tests/test_map_region.py` |

Adding a new step to the poster? Add it to `PRE_UPLOAD_STEPS` in `queue.py` if
it runs before `photo_upload`. `tests/test_failure_reporting.py` fails if you
forget.
