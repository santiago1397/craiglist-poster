# Craigslist Auto-Poster

Anti-detect Craigslist roofing ad poster for South Florida. Rotates 4 accounts
across machines — eight ads a weekday, two per account, one in the morning and
one in the afternoon — uses real Chrome via patchright, human-like typing, photo
+ content deduplication, strict cooldowns, and an anonymous ghost-check.

> **The posting model has changed.** `cl post` no longer generates an ad from
> `data/ads.xlsx` at post time. It claims a pre-written draft from the dashboard
> queue, and the **server** decides whether this machine may post and which
> account goes next. With no queue configured (`QUEUE_URL` / `MACHINE_TOKEN`) or
> an empty queue, `cl post` does nothing — posting is fail-closed on purpose.
>
> See [DESIGN.md](DESIGN.md) for the decisions behind this and what is still to
> come (AI-drafted copy, generated images, the studio module).

---

## One-time setup

```bash
uv sync
uv run patchright install chrome
```

Then:

1. **Create the Excel template** (or drop in your own with the same schema):
   ```bash
   uv run cl init-data
   ```
   Edit `data/ads.xlsx`. Schema:
   `county | city | service_offered | posting_title | zip_code | description | license_number | phone_number | photos_count`
   `posting_title` and `description` support spintax `{a|b|c}` and tokens
   `{city}`, `{county}`, `{zip_code}`, `{phone}`, `{license}`, `{service}`.

2. **Drop unique photos** in `data/photos/craigs1/`, `craigs2/`, `craigs3/`,
   `craigs4/`. No overlap — Craigslist detects reused images.

   **Cover images** (optional but recommended): drop edited "thumbnail" images
   in `data/covers/unclaimed/`. See [Cover images](#cover-images) below.

3. **Bind accounts to machines.** Edit `src/craigslist_auto/config.py` and set
   each account's `allowed_machine` to the hostname of the PC it should run on.
   Find your hostname with `hostname` in cmd. Override with `CL_MACHINE` env var
   if needed.

4. **Log into each account once** (opens Chrome — log in manually, close window):
   ```bash
   uv run cl init-account craigs1
   uv run cl init-account craigs2
   uv run cl init-account craigs3
   uv run cl init-account craigs4
   ```
   The login persists in `profiles/<account>/`. Don't delete that folder.

> **Note on OneDrive:** if the project lives in OneDrive, exclude `profiles/`
> and `data/covers/` from sync or move the project out. OneDrive will corrupt
> Chrome's profile lock files, and the cover-claim `shutil.move` calls will
> race OneDrive's sync process.

---

## Manual commands

All commands run via `uv run cl <command>`.

| Command | What it does |
|---|---|
| `cl init-data` | Create a sample `data/ads.xlsx`. |
| `cl init-account <name>` | Open Chrome with that account's profile so you can log in. |
| `cl status` | Ask the server which accounts can post right now, why not, and how deep each queue is. |
| `cl post` | Claim the next queued draft and post it. The server picks the account. No-ops if nothing is eligible or the queue is empty. |
| `cl post --account craigs1` | Restrict the claim to one account (still respects machine binding). |
| `cl post --dry-run` | Walk the form without publishing. Reads the head of the queue but **claims nothing**, so it never consumes a draft. |
| `cl post --headless` | Run browser headless (not recommended — easier to detect). |
| `cl post --draft-id <id>` | Post one specific draft. Used by the dashboard's **Post now**; you rarely type it. Refuses unless that draft carries a live request. |
| `cl check-ghosts --proxy http://host:port` | Check whether recent posts are visible in public search, from a different network (phone hotspot or any proxy you supply). |
| `cl check-ghosts --allow-local-ip` | Same check from this machine's IP. Weaker — CL shows you your own ghosted posts. |
| `cl edit` | Run one pass of pending edit work: load posts the dashboard asked for, apply queued changes. The reporter daemon does this every 15s anyway. |
| `cl edit --dry-run` | Rehearse an edit: open the form, diff against desired, **type nothing**. Safe against live posts. |
| `cl edit-canary <post_id>` | Perform one *real* edit end to end. Refuses any post not in `CL_CANARY_POSTS`. |
| `cl scan-ended` | Read the account page's inactive and deleted tabs and report every ended posting, so an ad that has expired stops being a bare id. |

---

## Editing live posts

A post used to be write-once. Now **Posts → click any post** opens its own page,
where you edit it with the same form Review uses for drafts — copy, images and
all — and the desktop applies the change when it next has a free browser.

There is no separate Edits tab any more. "Change this ad" is one question, and
it should not depend on whether the ad has published yet.

The flow is asynchronous end to end — nothing here happens the moment you click:

1. **Load** — the desktop opens the post's real Craigslist edit form and reports
   its current content back. The dashboard has never stored post bodies, so this
   is the only way to know what a post actually says. Nothing is editable until
   you have done this once.
2. **Edit** — you change the copy or the images. That records *desired state*,
   not a job: editing twice before the desktop runs just supersedes, it doesn't
   queue twice.
3. **Reconcile** — the desktop takes the browser lease, re-reads the form, checks
   it still matches what you were looking at, and applies the change.

If the live post moved underneath you, the edit **parks** instead of clobbering
it. If a reconcile fails after touching the images, the post is flagged
`degraded_live` — that means a live posting is in a worse state than before and
needs you. Both surface on the post's own page, and as counts on **Posts** and
in **Diagnostics → Editing live posts**.

> **Editing is now ON, and the selectors behind it have never been run against
> the real form.** Migration 0015 flips `edits_enabled` to true. The Craigslist
> edit-form selectors in `src/craigslist_auto/editor.py` are still inferred, so
> work through the ladder in [DESIGN_EDITS.md](DESIGN_EDITS.md) — spike,
> `cl edit --dry-run`, then `cl edit-canary` on a throwaway post — before
> letting the daemon run against real inventory. Set `CL_EDIT_TRACE=1` while you
> do: it captures the form on success as well as failure, and every attempt
> records which selectors matched.
>
> To stop it at any time, without a deploy: **Settings → Guardrails**, or
> `UPDATE guardrail_settings SET edits_enabled = FALSE, edits_paused_reason = '…'`.

**Debugging an edit.** Every step of a run records the page it was actually on —
title and URL — on the attempt's step trail, visible under the post's Edit
history. That is usually enough: three of the first bugs here were the browser
sitting on a page the code did not expect.

| `CL_EDIT_TRACE` | What gets captured |
|---|---|
| unset | screenshot + HTML on failure only |
| `1` | the above, plus the landing page, both forms, the gallery, and both writes |
| `all` | the above, plus every single step — a filmstrip of the whole walk |

A failed run also uploads the tail of `logs/run.log` as an artifact, so the
desktop's own log is readable from the dashboard without going to the machine.

**How text is entered.** The editor pastes by default (`CL_EDIT_TYPING=paste`).
Set `CL_EDIT_TYPING=human` to type short fields character by character the way
posting does; the body is pasted either way, because 14,500 characters at
human speed is over half an hour for one field.

Posting deliberately still types. A new listing is what Craigslist scrutinises
and that flow has run this way for months; an edit is a short visit to an ad
that already exists, and it holds the browser lease while it runs.

### Ended postings

A posting that ends takes its copy with it. Craigslist stops serving the public
URL and stops offering the edit form, and hydration — the only route to a body —
goes with it. Anything published before the image stack existed left no record
of its pictures either.

Two things now guard against that:

- **What goes out is kept.** A post published from the queue copies the draft's
  copy onto itself, and the pictures it used stay reachable from the draft that
  produced it. Both show on the post's page, from our own bytes rather than
  Craigslist's, which stop loading when the ad ends.
- **`cl scan-ended`** walks the account page's inactive and deleted tabs and
  reports what is there — titles, dates, area, category, final counters. It is
  the last place an ended ad is described at all.

Neither is retroactive. Postings that ended before this existed have only their
title and URL, and no route back to the rest.

**County and service type are not editable on a live post.** Craigslist's edit
form exposes no control for either, so offering them would stage a change the
desktop can never make. Anything else it cannot reach fails loudly rather than
reporting success — see `unsupported_field` in Diagnostics.

**Editing has its own switch, independent of posting.** Pausing posting stops
posting; it does not stop editing. The two carry different risk — posting
creates listings and burns the daily and weekly caps, editing changes an ad that
is already up against its own caps — and "stop posting while I fix an ad" is an
ordinary thing to want. To stop everything, turn off both under
**Settings → Guardrails**.

Edits have their own guardrails, all editable under **Settings → Editing live
posts**: an edit window, a per-account daily cap, a per-post cooldown, and a
lifetime cap. They are deliberately far looser than posting's, because editing
your own ad is ordinary behaviour and you drive it by hand — the thing that
stops a broken selector retrying forever is the per-post cooldown, not the daily
cap. **Apply now** skips the window and the cooldown; it never skips the caps or
the switch, and it tells you at the click if something will stop it. Failed attempts count against
the cap on purpose, so a broken selector can't retry all day. The desktop clamps
whatever the server sends to ceilings compiled into `config.py`.

### The browser lease

`post`, `stats-sync` and `edit` all drive Chrome against the same
`profiles/<account>/` directory, which Chrome locks exclusively. They now share a
machine-wide lease (`data/browser.lock`). Posting and stats block on it; editing
takes it opportunistically and skips if it's held, and refuses to start within 10
minutes of a posting slot. `cl status` shows the current holder.

### Failure artifacts

When an edit fails, the desktop captures a screenshot and an HTML dump and
uploads them to the dashboard, where they're linked from the post's History.
That's the difference between "TimeoutError waiting for selector" and seeing the
page Craigslist actually served.

### Eligibility rules

Evaluated **on the server**, not on this machine. An account can post only if
**all** of these pass:

- Current time (America/New_York) is between **8 AM and 6 PM**.
- Current day is **Monday through Friday**.
- Fewer than **9 posts in the last 24h across all accounts**.
- This account has posted fewer than **2 times today**.
- This account has fewer than **11 posts in the last 7 days**.
- At least **5 hours** since this account's last post.
- The account has at least one queued draft.
- No post is already in flight for this account.
- The account's `allowed_machine` matches the current machine.

> **Two of those numbers are one higher than the figure they enforce, and that
> is deliberate.** The 24-hour and 7-day caps are counted over a **rolling
> window**, not a calendar day or week. A post lands a few minutes after its
> scheduled fire, so yesterday's post at the same clock time is always still
> inside the window — at every fire the rolling count already reads 8. Set the
> daily cap to 8 and *every* fire is refused, which looks exactly like a broken
> scheduler. Hence 9 for 8 ads a day, and 11 for 10 a week.
>
> The per-account daily cap is the exception: it is a **calendar day** in ET, so
> it is set to the number it means. That is what makes "two per account" a rule
> rather than something inferred from the cooldown.

Tune all of these in the dashboard under **Settings → Guardrails**. The desktop
also clamps whatever the server sends to ceilings compiled into
`src/craigslist_auto/config.py` (max 10/day, max 2/account/day, max 12/week,
minimum 5h cooldown, 06:00–22:00) — but be clear on what that does: it is an
**alarm, not a gate**. Only the reporter daemon reads it, and all it does is
report a `flow_error` you will see in Diagnostics. The server is authoritative
for whether a post goes out. Keeping each ceiling one notch above the live value
is what makes a mistyped setting show up quickly instead of quietly changing
behaviour overnight. Raising a ceiling is a deliberate code change and redeploy.

---

## Posting one draft on demand

**Review → Post now** on any queued draft publishes that one without waiting for
the next scheduled slot. The reporter daemon picks the request up within ~15
seconds and runs the ordinary posting flow against that draft.

It changes *when* a post is attempted and *which* draft goes — never whether it
is allowed. Every guardrail below still applies, evaluated on the server exactly
as for a scheduled fire, and the post counts against the day's cap like any
other. If
the account cannot post right now you are told why immediately and nothing is
queued, so a click can never surface as a surprise post hours later.

Requires the reporter daemon to be running on the posting machine — that is what
polls for the request. If it is down, the request expires after 20 minutes and
says so on the draft.

---

## Run it automatically every day

A Scheduled Task fires `cl post` **eight times a weekday (Mon-Fri)** — hourly
from **8am to 11am**, then hourly from **2pm to 5pm**. One ad per fire, four
accounts, two each: the server's longest-idle-first rotation gives every account
one morning fire and one afternoon fire without the schedule having to know
which is which. The midday gap is what makes the pairing land six hours apart,
an hour clear of the 5-hour cooldown.

A fire that can't post no-ops — that's intentional, the script self-throttles.

Three places describe this same schedule and must stay in step:
`scripts/install-schedule.ps1` (the trigger itself), `TASK_FIRE_HOURS` in
`backend/app/services/queue.py` (the forecast on Review), and
`POSTING_SLOT_HOURS` in `src/craigslist_auto/edit_worker.py` (the guard that
keeps an edit from starting just before a fire).

### Start the background task

```powershell
# In PowerShell, from the project root:
.\scripts\install-schedule.ps1
```

The script:
- Finds `uv.exe` on your PATH
- Registers a task named **"CL Auto Post"**
- Runs as the current user, only while you're logged in (the browser needs a desktop)
- Re-installing overwrites the existing task, so it's safe to run again

### Stop the background task

Three ways, easiest first:

**1. Run the uninstall script:**
```powershell
.\scripts\uninstall-schedule.ps1
```

**2. PowerShell one-liners:**
```powershell
# Pause it (can re-enable later)
Disable-ScheduledTask -TaskName "CL Auto Post"

# Resume
Enable-ScheduledTask -TaskName "CL Auto Post"

# Remove completely
Unregister-ScheduledTask -TaskName "CL Auto Post" -Confirm:$false
```

**3. Task Scheduler GUI:**
Press `Win+R`, type `taskschd.msc`, find **"CL Auto Post"** in the task list,
right-click → **Disable** or **Delete**. To kill a currently-running post,
right-click → **End**.

### Verify it's installed

```powershell
Get-ScheduledTask -TaskName "CL Auto Post"
Get-ScheduledTaskInfo -TaskName "CL Auto Post"   # last run time, result
```

---

## When something breaks

Open **Diagnostics** in the dashboard. It merges everything that can go wrong —
failed posting runs, background job failures, drafts stuck in a claim, and
machines that have stopped reporting — each with a plain-English explanation and
the screenshot of the page Craigslist actually served.

See [DIAGNOSTICS.md](DIAGNOSTICS.md) for how to read it. The short version:

- **Every failure reports itself**, over a durable outbox that survives outages.
  If it isn't in Diagnostics, the desktop never got that far.
- **A posting failure names the step it died on.** Before `photo_upload` nothing
  was consumed and the draft returns to the queue automatically. At or after it,
  images were burned and the draft parks in Review for you.
- **A post can publish and still be wrong** — missing photos, a guessed county,
  an unresolvable URL. Those stay `posted` so the cooldown maths is right, and
  are flagged critical so a green badge never hides a broken ad.
- **A machine that goes silent is itself a critical problem.** Nothing else
  catches it, because every other signal needs the desktop to report it.

From the desktop, `uv run cl status` answers the same question locally.

---

## Verifying posts

After the scheduler has run, use these to confirm posts went out and aren't ghosted.

### Did the posts succeed?

```bash
uv run cl status          # which accounts posted recently, when each can post next
type data\state.json      # every successful post: account, url, title, timestamp
type logs\run.log         # full action log (errors, retries, selector misses)
```

`data/state.json` is the source of truth — if a post URL is in there, the form was
submitted successfully.

### Are the posts visible (not ghosted)?

There is no built-in proxy — you supply the exit. Use a phone hotspot or any
non-home proxy so the search runs from a different IP than the one that posted:

```bash
uv run cl check-ghosts --proxy http://host:port
```

Without `--proxy` the command **refuses to run**, because a check from the
posting machine's own IP isn't trustworthy — Craigslist keeps showing you your
own posts after they're ghosted for everyone else. If you want that weaker
check anyway:

```bash
uv run cl check-ghosts --allow-local-ip
```

Results append to `logs/ghost_check.jsonl` (one JSON line per check with
`visible: true/false` and `proxied: true/false`) and update each account's
ghost count in state. Treat `visible: true` with `proxied: false` as
"not proven ghosted", not as "confirmed live".

```bash
type logs\ghost_check.jsonl
```

### Manual sanity check

Open a URL from `data/state.json` in an **incognito window on a different
network** (phone hotspot). Then search Craigslist for the title keywords. If
the direct URL loads but the ad doesn't show up in search → it's ghosted.

---

## Images — the two stacks

Images live on the **dashboard**, not on this machine. The desktop downloads
whatever the server attached to the draft it claimed. The local
`data/covers/` folders are from the retired model and are no longer read.

There are two separate stacks, because they are two separate decisions:

| | Cover stack | Photo stack |
|---|---|---|
| Goes in | slot 1 — the Craigslist thumbnail | slots 2-24 |
| Chosen | by hand, in Review | in bulk, by Autofill or generation |
| Looks like | a photo with a CTA + phone composited on by Pillow | a plain roof shot |

The split is enforced, not advisory: slot 1 refuses a photo and the photo slots
refuse a cover. Use **Make this a cover / Make this a photo** on the Images page
to move one across.

### The five buckets

Each stack shows the same five, and an image is in exactly one:

| Bucket | Meaning |
|---|---|
| **Pending review** | freshly generated, unusable until approved |
| **Available** | approved and free to be attached |
| **Assigned** | reserved by a queued draft or a live posting's pending edit |
| **Published** | Craigslist has seen it; blocked until its reuse cooldown expires |
| **Rejected** | you said no |

**Assigned means reserved.** An image attached to a live draft is not offered to
any other draft. You can still force it — the picker shows reserved images
greyed with the draft holding them, and clicking asks you to confirm — but it
can no longer happen by accident, which it used to do on every top-up run.

### Reuse — two settings, and what they cost

Two rules used to be fixed in code and are now under **Settings → Guardrails**:

| Setting | Ships as | Was |
|---|---|---|
| `image_reuse_cooldown_days` | **7** | 30 |
| `image_owner_binding` | **off** | permanently on |

Together they decided how big the photo pool had to be. At ~166 photos a day, a
30-day cooldown needs ~4,980 standing photos, and the per-account claim then
split that four ways. At 7 days with no account binding the same queue runs on
roughly **1,160 photos shared across all four accounts** — which is what makes
importing a few hundred real job-site pictures a workable alternative to
generating thousands.

> **Know what this trades away.** A Craigslist ad stays live about 30 days, so a
> 7-day cooldown means the same photo is *expected* to be visible on two ads
> under two different accounts at once. Duplicate images across sellers is the
> classic reason posts get ghosted. This was chosen deliberately, with the
> reasoning in [DESIGN.md](DESIGN.md).
>
> **If posts start disappearing, these are the first two things to move.** Raise
> the cooldown toward 30 and switch `image_owner_binding` back on. Claims
> written before the change were never deleted, so turning binding back on
> re-enforces them immediately — no deploy, no data migration.

Note the accounts are already linkable by other means: every ad body carries the
same `LICENSE_NUMBER`, and tracking numbers rotate across accounts rather than
being bound to one. Photo reuse adds a signal rather than creating the first one.

## Photos from CompanyCam

Your crews' own job-site photos can fill the photo slots instead of generated
roof shots. The importer is a command run on the server, not a dashboard button:

```bash
# how many photos are there? downloads nothing
docker compose exec api python -m app.importers.companycam_import \
    --count-only --token "$TOK"

# pull a batch — everything lands on the pending shelf for review
docker compose exec api python -m app.importers.companycam_import \
    --limit 150 --token "$TOK"

# review from the command line, since the Images grid tops out around 200 rows
docker compose exec api python -m app.importers.curate list    --source companycam --limit 20
docker compose exec api python -m app.importers.curate approve --source companycam --limit 50 --yes
```

Nothing it imports can publish until you approve it — imports land `pending`
like generated images do.

**Every photo is re-encoded on the way in** to a 1600px JPEG at q85 (~250–400KB).
That is not just to save space:

- iPhone originals are **HEIC**, which the storage layer would file as `.bin` and
  the desktop would then hand to Craigslist's upload control at post time — so
  the failure would land on a live account hours after the import.
- Phone photos rely on an EXIF `Orientation` tag. Stripping EXIF without
  applying it first publishes a large share of them **sideways**, permanently.
- These are photographs of customers' homes and carry **GPS coordinates**. The
  re-encode drops the whole EXIF block.

Re-running is free: the `image_sources` ledger keys on CompanyCam's own photo id,
so nothing is downloaded twice, and a photo you rejected or deleted stays gone
rather than reappearing on the next run.

`--token` exists because `docker compose` bakes `env_file` at container creation,
so a token added to `.env.prod` is invisible until you restart. Filters:
`--start-date`, `--end-date`, `--project-ids`, `--tag-ids`, `--limit`.

Photos still come in as `kind='photo'` — **the cover stack is untouched** and
covers stay hand-picked. Promote one with "Make this a cover" on the Images page.

### Per-post logic

- Generation attaches **23 photos** and **no cover**, so every queued draft
  visibly wants a thumbnail from you.
- Roughly **1 in 10** drafts (`imageless_rate`) take no images at all, on
  purpose, so accounts do not look mechanically identical. Those get no cover
  either.
- **Autofill 23 photos** on a draft in Review tops up empty photo slots. It
  never replaces what you already chose and never touches slot 1.
- If you never pick a cover, one is chosen **automatically at claim time** and
  reported in Diagnostics. If the cover stack is empty, the first photo becomes
  the thumbnail — which is why an empty cover stack is a critical problem.

### Keeping the stacks full

At eight posts a day, 24-image ads burn about **166 photos a day** (8 × 23,
less the ~1-in-10 imageless roll). The standing pool you need is that figure
times the reuse cooldown:

| `image_reuse_cooldown_days` | Standing pool |
|---|---|
| 30 (the old fixed value) | ~4,980 |
| 14 | ~2,320 |
| **7 (ships)** | **~1,160** |

With `image_owner_binding` off that pool is shared across all four accounts
rather than split into per-account quarters, so ~1,160 is the whole requirement
— not 1,160 each. That is the number to plan against; the "photo stack short by
N" line on the Images page counts the reservation the current queue needs, which
is a much smaller figure.

**Covers are hand-picked and now run to 8 a day, 40 a week.** If the cover stack
empties, slot 1 goes out empty and the first roof photo becomes the Craigslist
thumbnail — the worst quality failure in this system. It reports itself as a
`cover_auto_chosen` entry in Diagnostics every time, so falling behind is loud.

Generation is manual — press **Generate** on the Images page, or upload. Drafts
fill with whatever exists and publish thinner; nothing blocks.

A background refill loop exists and is **off by default**, which at this volume
is a deliberate choice rather than an oversight: the image prompts are still
being tuned and there is no point spending on output you don't want yet. Turn on
`image_topup_enabled` under **Settings → Generation** when they're settled; it
generates photos straight into Available (covers always stay manual) whenever
depth drops below `image_stack_floor`. At $0.0035 an image and 166 a day, steady
state is roughly **$18/month**.

## Switching the model providers

**Settings → Draft generation** picks who writes the copy and who draws the
pictures. Both default to MiniMax; OpenAI is configured and selectable. Each
provider carries its own model, endpoint, cost and API key, so switching is one
dropdown and switching back loses nothing.

Keys are entered in the panel and stored **encrypted** — the field is
write-only, showing a last-four fingerprint rather than the key, and nothing
sends one back to the browser. If a provider's key is missing the save is
refused, naming the environment variable that would also fix it
(`MINIMAX_API_KEY`, `OPENAI_API_KEY`). Those variables remain a permanent
fallback, read whenever nothing is stored.

Two things are worth knowing before you switch the image provider:

- **Set the cost.** It is stamped on every image and totalled per API key on the
  Settings page, and that total is the only control on agent image generation,
  which is deliberately uncapped. The seeded OpenAI figure is a high placeholder
  on purpose — a low one would under-report spend silently. Replace it with the
  real price for the quality tier you settle on.
- **Photos are the bill, not covers.** Covers run ~8 a day; photos run ~166. At
  present that is dormant because `image_topup_enabled` ships off, so photos are
  only generated when you press Generate. Turning it on at a premium provider's
  rates is a budget decision worth making deliberately.

OpenAI has no 4:3 size, so its adapter requests 1536×1024 and crops to
1365×1024. That is not cosmetic: Craigslist's own display variant is 1200×900,
and an image at any other ratio gets cropped by the site instead — which on a
cover takes the composited phone number with it.

The encryption key is derived from `JWT_SECRET`. Rotating it makes the stored
provider keys unreadable; the dashboard keeps working and generation falls back
to the environment variables, and re-entering the keys is the whole fix. See
[DESIGN_PROVIDERS.md](DESIGN_PROVIDERS.md) for that and the rest of the
reasoning, including why Gemini ("nano banana") has a seam but no adapter yet.

---

## Letting an AI read the system

**Settings → API keys** issues a key for the `/agent` API — a read-only view of
everything above, shaped for an AI assistant rather than a browser. Hand it one
URL and it works out the rest:

```
https://api.craigslist.santiagoproperties.uk/agent/help?key=<KEY>
```

That returns a plain-English manual of every question it can ask. Answers come
back as prose by default (`&format=json` if something needs to parse one), with
the caveats welded into the sentences — that stats are a once-a-day scrape, that
post times are forecasts, that no errors does not mean healthy.

It can read status, the queue, published posts, performance, problems, raw error
logs and image inventory.

With an **`agent`-scope key** it can also compose: generate an image from a
prompt, write a draft, place it somewhere you are not already advertising,
attach a cover and fill the photo slots. What it cannot do is decide that any of
that goes out. A draft an agent writes is **unreviewed**, no API route can
change that, and an unreviewed draft cannot publish — so the worst case is copy
you read and delete, never an ad you have to take down. It can approve images it
generated itself and nothing else, and it cannot edit live ads or change
guardrails.

Image generation through such a key is not capped. Settings → API keys shows how
many images each key has generated and what they cost.

Publishing a draft **you have already marked reviewed** needs a `post`- or
`agent`-scope key and still passes every guardrail — see [AGENTS.md](AGENTS.md).

For a shell or your own terminal there is a CLI, and for MCP-capable hosts an
MCP server. Both are single files in `tools/` with no dependencies beyond the
standard library:

```bash
export CL_AGENT_KEY=<key>
python tools/cl_agent.py status
python tools/cl_agent.py stats --window 7d
```

---

## Where things live

| Path | What |
|---|---|
| `data/ads.xlsx` | Your ad rows (seed briefs for generation). |
| `data/image_cache/` | Images downloaded from the dashboard, named by sha256. Safe to delete. |
| `data/photos/<account>/`, `data/covers/` | Retired local pools. The queue no longer reads these. |
| `profiles/<account>/` | Persistent Chrome profile per account. Don't delete. |
| `data/state.json` | Post history (used for cooldowns + ghost checks). |
| `logs/run.log` | Rotating log of every run. |
| `logs/photo_usage.json` | Per-photo last-used timestamp (30-day cooldown). |
| `logs/content_hashes.json` | Hashes of posted title+body (60-day dedup). |
| `logs/ghost_check.jsonl` | Append-only ghost-check results. |

---

## Troubleshooting

- **`cl status` says no machine matches** — edit `allowed_machine` in
  `config.py`, or set `CL_MACHINE=<name>` env var.
- **Selectors broken on the posting form** — Craigslist changes them
  occasionally. Run `cl post --dry-run` with `headless=False` and update the
  selectors in `src/craigslist_auto/poster.py`.
- **All posts ghosted** — likely cause: duplicate content (identical keyword
  blocks across ads) or reused photos. Vary the spintax more, rotate phone
  numbers, and confirm photos in each account folder are unique.
- **OneDrive errors on profile files** — exclude `profiles/` from sync.
