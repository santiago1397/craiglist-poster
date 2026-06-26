# Craigslist Auto-Poster

Anti-detect Craigslist roofing ad poster for South Florida. Rotates 3 accounts
across machines, uses real Chrome via patchright, human-like typing, photo +
content deduplication, strict cooldowns, and an anonymous ghost-check.

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
> from sync or move the project out. OneDrive will corrupt Chrome's profile
> lock files.

---

## Manual commands

All commands run via `uv run cl <command>`.

| Command | What it does |
|---|---|
| `cl init-data` | Create a sample `data/ads.xlsx`. |
| `cl init-account <name>` | Open Chrome with that account's profile so you can log in. |
| `cl status` | Show which accounts are eligible to post right now and why not. |
| `cl post` | Post one ad now. Picks the next eligible account automatically. |
| `cl post --account craigs1` | Force a specific account (still respects machine binding). |
| `cl post --dry-run` | Walk through the form without publishing. Use this to verify selectors. |
| `cl post --headless` | Run browser headless (not recommended — easier to detect). |
| `cl check-ghosts` | Check whether recent posts are visible in public search. |
| `cl check-ghosts --proxy http://host:port` | Ghost-check from a different network (phone hotspot, residential proxy). True external check. |

### Eligibility rules

An account is eligible only if **all** of these pass:

- Current local time is between **8 AM and 7 PM**.
- Fewer than **2 posts in the last 24h across all accounts**.
- This account has fewer than **3 posts in the last 7 days**.
- At least **48 hours** since this account's last post.
- The account's `allowed_machine` matches the current machine.

Tune these in `src/craigslist_auto/config.py`.

---

## Run it automatically every day

A Scheduled Task fires `cl post` at **8am, 11am, 2pm, 5pm**. Most fires no-op
because of the cooldowns — that's intentional. The script self-throttles.

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

Same-network check (fast, but Craigslist often shows your own posts to you even
when they're ghosted to everyone else):

```bash
uv run cl check-ghosts
```

True external check — required for a reliable answer. Use a residential proxy or
your phone hotspot:

```bash
uv run cl check-ghosts --proxy http://host:port
```

Results append to `logs/ghost_check.jsonl` (one JSON line per check with
`visible: true/false`) and update each account's ghost count in state.

```bash
type logs\ghost_check.jsonl
```

### Manual sanity check

Open a URL from `data/state.json` in an **incognito window on a different
network** (phone hotspot). Then search Craigslist for the title keywords. If
the direct URL loads but the ad doesn't show up in search → it's ghosted.

---

## Where things live

| Path | What |
|---|---|
| `data/ads.xlsx` | Your ad rows. |
| `data/photos/<account>/` | Unique photos per account. |
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
