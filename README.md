# Craigslist Auto-Poster

Anti-detect Craigslist roofing ad poster for South Florida. Rotates 3 accounts
across machines, uses real Chrome via patchright, human-like typing, photo +
content deduplication, strict cooldowns, and an anonymous ghost-check.

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

2. **Drop unique photos** in `data/photos/craigs1/`, `craigs2/`, `craigs3/`.
   No overlap — Craigslist detects reused images.

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

---

## Editing live posts

A post used to be write-once. Now the dashboard's **Edits** tab can change one,
and the desktop applies it when it next has a free browser.

The flow is asynchronous end to end — nothing here happens the moment you click:

1. **Load** — the desktop opens the post's real Craigslist edit form and reports
   its current content back. The dashboard has never stored post bodies, so this
   is the only way to know what a post actually says.
2. **Edit** — you change the title/body. That records *desired state*, not a job:
   editing twice before the desktop runs just supersedes, it doesn't queue twice.
3. **Reconcile** — the desktop takes the browser lease, re-reads the form, checks
   it still matches what you were looking at, and applies the change.

If the live post moved underneath you, the edit **parks** instead of clobbering
it. If a reconcile fails after touching the images, the post is flagged
`degraded_live` — that means a live posting is in a worse state than before and
needs you.

> **Editing ships disabled.** `edits_enabled` defaults to false. Nothing will
> touch a live posting until you turn it on under **Settings → Guardrails**, and
> you should not do that until the phase-0 spike in
> [DESIGN_EDITS.md](DESIGN_EDITS.md) is done — the Craigslist edit-form
> selectors in `src/craigslist_auto/editor.py` are inferred, not observed.

Edits have their own guardrails, mirroring posting: an edit window, a per-account
daily cap, a per-post cooldown, and a lifetime cap. Failed attempts count against
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

- Current time (America/New_York) is between **8 AM and 7 PM**.
- Current day is **Monday through Friday**.
- Fewer than **3 posts in the last 24h across all accounts**.
- This account has fewer than **7 posts in the last 7 days**.
- At least **20 hours** since this account's last post.
- The account has at least one queued draft.
- The account's `allowed_machine` matches the current machine.

Tune the first five in the dashboard under **Settings → Guardrails**. The
desktop clamps whatever the server sends to hard ceilings compiled into
`src/craigslist_auto/config.py` (max 5/day, max 10/week, minimum 18h cooldown,
06:00–22:00) and reports a `flow_error` when it has to clamp — so a mistyped
setting can't get an account banned. Raising a *ceiling* is a deliberate code
change and redeploy.

---

## Posting one draft on demand

**Review → Post now** on any queued draft publishes that one without waiting for
the next scheduled slot. The reporter daemon picks the request up within ~15
seconds and runs the ordinary posting flow against that draft.

It changes *when* a post is attempted and *which* draft goes — never whether it
is allowed. Every guardrail below still applies, evaluated on the server exactly
as for a 9am fire, and the post counts against the day's cap like any other. If
the account cannot post right now you are told why immediately and nothing is
queued, so a click can never surface as a surprise post hours later.

Requires the reporter daemon to be running on the posting machine — that is what
polls for the request. If it is down, the request expires after 20 minutes and
says so on the draft.

---

## Run it automatically every day

A Scheduled Task fires `cl post` at **9am, 1pm, 5pm** on **weekdays (Mon-Fri)**.
Most fires no-op because of the cooldowns — that's intentional. The script
self-throttles.

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
| **Published** | Craigslist has seen it; blocked for a 30-day cooldown |
| **Rejected** | you said no |

**Assigned means reserved.** An image attached to a live draft is not offered to
any other draft. You can still force it — the picker shows reserved images
greyed with the draft holding them, and clicking asks you to confirm — but it
can no longer happen by accident, which it used to do on every top-up run.

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

24-image posts need roughly **1,035 standing photos** and about **69 a day**.
Generation is manual — press **Generate** on the Images page, or upload — and
the Images page carries a running "photo stack short by N" line so the gap is
never silent. Drafts fill with whatever exists and publish thinner; nothing
blocks.

A background refill loop exists and is **off by default**. Turn on
`image_topup_enabled` under **Settings → Generation** once the image prompts are
settled; it generates photos straight into Available (covers always stay manual)
whenever depth drops below `image_stack_floor`. At $0.0035 an image, steady
state is roughly $7/month.

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
