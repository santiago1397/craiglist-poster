# Giving an AI access to this system

This describes the `/agent` API — a read-only view of the poster that an AI
assistant can use, plus one guarded write.

There are two audiences here. If you are a person setting this up, read
[Issuing a key](#issuing-a-key). If you are an AI that has been handed a key,
you do not need this file at all — fetch `/agent/help` and it will tell you
everything, including things this file may have gone stale on.

---

## The one-line version

```
https://api.craigslist.santiagoproperties.uk/agent/help?key=<KEY>
```

That URL returns a plain-English manual listing every question that can be
asked and the exact URL to ask it with. It is generated from the live route
table, so it cannot describe an endpoint that does not exist or miss a
parameter that does.

Everything else is one more URL of the same shape.

---

## Issuing a key

**Settings → API keys → Create key.** The key is shown once. Three scopes:

| Scope | What it opens | May travel in a URL |
|---|---|---|
| **Read only** | Every `/agent/*` GET. Cannot change anything. | yes |
| **Read + publish** | The above, plus `POST /agent/post-now` for drafts already marked reviewed. | no |
| **Read + compose + publish** | The above, plus writing drafts, generating images and attaching them. | **no, not even on reads** |

Revoke any of them from the same page. `last used` shows whether a key is still
live, so a forgotten one is visible rather than silently valid forever. For
`agent` keys the page also shows how many images that key has generated and what
they cost, because generation is not capped.

### Why read keys go in the URL and post keys do not

Many AI fetch tools cannot set HTTP headers — Claude Code's `WebFetch` cannot.
An API that demands `Authorization: Bearer` is one those agents cannot call at
all. So reads accept `?key=`.

The cost is real: a key in a URL is written to the server access log, the
client's history, and any proxy in between. Two things follow from that, and
both are deliberate:

- Read keys are treated as low-value and expected to be rotated. A leaked read
  key exposes information; it cannot publish, spend, or change a setting.
- **The publish scope is header-only.** A `POST` carrying `?key=` is rejected
  with a 400 telling the caller to move it into `X-API-Key` — not silently
  accepted from the header instead, because by then the secret is already in
  the log. Anything capable of issuing a POST is capable of setting a header,
  so the constraint that forced the query-string concession never applied here.

The server also redacts `key=` from its own uvicorn access log
(`_RedactApiKeyFilter` in `backend/app/main.py`). That covers our logs, not the
client's.

**The `agent` scope is refused in the query string on every verb, reads
included.** The concession above was bought with a specific argument — a leaked
read key exposes information and nothing else, so "rotate it" is a complete
answer. A key that can also publish does not qualify for that bargain, and
honouring it from the header on a request that also carried `?key=` would leave
the secret in the log anyway. An agent that genuinely cannot set headers gets a
read key. The CLI and MCP server both send headers already, so neither notices.

---

## Three ways in, one API

All three call the same endpoints. Pick by what the agent can do, not by which
is newest.

| | Use when | Needs |
|---|---|---|
| **Plain HTTP** | the agent can fetch a URL and nothing else | nothing |
| **CLI** (`tools/cl_agent.py`) | the agent has a shell, or you want to check something yourself | Python 3.9+ |
| **MCP** (`tools/cl_agent_mcp.py`) | the host speaks MCP (Claude Code, Claude Desktop) | Python 3.9+, one config entry |

Both tools are single files with **no dependencies beyond the standard
library** — no `pip install`, no virtualenv. Copy either one anywhere Python
runs.

### CLI

```bash
export CL_AGENT_KEY=<key from Settings -> API keys>
python tools/cl_agent.py help          # the server's own manual
python tools/cl_agent.py status
python tools/cl_agent.py stats --window 7d
python tools/cl_agent.py logs --hours 48 --flow post
python tools/cl_agent.py post-now 123  # needs a 'post'-scope key
```

Composing needs an `agent`-scope key:

```bash
python tools/cl_agent.py locations
python tools/cl_agent.py generate-image --prompt "a tile roof in Davie" --kind cover
python tools/cl_agent.py approve-image 412
python tools/cl_agent.py draft-create ./draft.json   # or '-' to read stdin
python tools/cl_agent.py draft-cover 88 412
python tools/cl_agent.py draft-autofill 88
python tools/cl_agent.py draft-show 88
```

`draft-create` takes a file, `-` for stdin, or inline JSON. A file is the normal
path: ad copy is thousands of characters with newlines in it, and shell quoting
mangles it. `cl_agent.py help` lists every field.

Add `--json` to any read command. `CL_AGENT_URL` overrides the host.

**The CLI always sends the key in a header**, never in the URL — a shell can
set headers, so there is no reason to use the leaky path. Nothing it does puts
the key in an access log or your shell history. A test enforces this.

Refusals exit non-zero and print the server's own wording, so a script that
checks the exit code cannot mistake a blocked post for a published one.

### MCP

```json
{
  "mcpServers": {
    "craigslist": {
      "command": "python",
      "args": ["tools/cl_agent_mcp.py"],
      "env": { "CL_AGENT_KEY": "<key>" }
    }
  }
}
```

Fifteen tools arrive named, described and typed, so the model never builds a
URL. The descriptions carry the same caveats the prose does — that
`craigslist_post_now` publishes a real advert, cannot be undone, and should be
confirmed with the user first; that `craigslist_generate_image` spends money;
and that `craigslist_create_draft` produces something **unreviewed that cannot
publish**, so a model must not report it as live. A test asserts each of those
clauses is still in its description, because the description is the only thing
guaranteed to be read before a tool is chosen.

---

## What an agent can see

| Endpoint | Question it answers |
|---|---|
| `/agent/help` | What can I ask? |
| `/agent/status` | Can each account post right now, why not, when is the next post, are the machines alive? |
| `/agent/queue` | What is waiting to publish, what is it missing, roughly when does it go? |
| `/agent/posts` | What has published, is it still live, is it ghosted, is an edit stuck? |
| `/agent/stats` | Views and impressions earned yesterday / last 7d / last 30d. |
| `/agent/problems` | What is broken, ranked, explained. |
| `/agent/logs` | The raw error records underneath that. |
| `/agent/inventory` | Are there enough images to fill the queue? |
| `/agent/locations` | Where may an ad go, and where are we already advertising? |

Responses are plain English by default. Add `&format=json` when something needs
to parse one — both render from the same data, so they cannot disagree.

---

## What an agent can compose

With an `agent`-scope key it can build a complete draft: generate an image from
a prompt, write the copy, place it in a location, attach a cover and fill the
photo slots.

| Endpoint | What it does |
|---|---|
| `POST /agent/images/generate` | Generate images from a prompt. Spends money. Lands them `pending`. |
| `POST /agent/images/{id}/approve` | Approve an image — **only one this key generated**. |
| `POST /agent/drafts` | Write a draft. Always `reviewed = false`. |
| `PATCH /agent/drafts/{id}` | Change a draft this key wrote. |
| `GET /agent/drafts/{id}` | Read it back, with images and a similarity score. |
| `POST /agent/drafts/{id}/cover` | Put an approved cover in slot 1. |
| `POST /agent/drafts/{id}/autofill` | Fill the photo slots from the stack. |

Three limits are structural rather than advisory:

**It cannot mark a draft reviewed.** No route exposes the field,
`drafts.create_draft` forces it false for anything an agent writes, and
`post-now` refuses an unreviewed draft. Composing an ad and publishing an ad are
two different permissions, and only the first was granted. That is what makes
handing over a compose key reasonable: the worst case is copy you have to read
and delete, not an ad you have to take down.

**It can only approve images it generated itself.** `images.created_by_key_id`
records who made each row; a human's image answers no, and so does another key's.
An agent curates what it made and does not get an opinion on your stack. It also
cannot acquire rights over an existing image by regenerating it — `_store` is
content-addressed and skips a digest already held rather than re-stamping it.

**It can only edit drafts it wrote.** A draft created in the dashboard is not an
agent's to change.

**County and city are validated against `backend/app/reference.py`.** That list
is closed because `poster._select_subarea()` matches the county by substring to
pick the Craigslist subarea radio — an unrecognised value does not raise, it
falls through to the first radio and the ad publishes in the wrong place with
nothing reporting it. `/agent/locations` returns the valid set, flags counties
with no subarea mapping, and says which cities already carry ads.

**Generation is not capped.** It ships uncapped deliberately; the control is
visibility. Every image records the key that made it and its cost, and
Settings → API keys totals both per key.

### What it still cannot do

It cannot edit a live listing, change guardrails, mark anything reviewed, manage
keys or accounts, or approve an image it did not make. Those stay in the
dashboard whatever the key says. The only write that reaches Craigslist is
`post-now`, and it is fenced:

- The draft must already be marked **reviewed** by a human. An agent can decide
  *which approved draft goes next*, never *what the ad says*.
- Every guardrail is evaluated server-side exactly as for a scheduled fire —
  posting window, weekday rule, 9-per-rolling-24h cap, 2-per-account-per-day
  cap, 11-per-account-per-week cap, 5-hour cooldown. A refusal comes back as a
  409 with the reasons verbatim and
  an instruction not to retry, because a guardrail is not a transient error.
- The request is attributed as `agent:<key label>`, so a post that went out
  off-cadence can be explained later.

It publishes to a live classifieds site under a real licence number, and one of
only eight posting slots a day. That is why the review gate is not optional.

---

## Three things that mislead agents

These are written into `/agent/help` and repeated in the body of every affected
response, because a caveat that lives in a separate field gets dropped the
moment a model summarises.

**1. Stats are a daily scrape, not live telemetry.** `stats-sync` reads
Craigslist's own counters once a day at 06:00 ET on the posting desktop. Today
is never complete. A day the machine was off is a gap that shows up as a flat
period, not as missing data.

Counters are also cumulative, so a period figure is a *difference between two
snapshots*. `queries.py` exposes a `views_per_day` field that is a lifetime
average (`total ÷ days_active`) — `/agent/stats` deliberately never surfaces
it, because an agent asked "how many views yesterday" would report that number
and be confidently wrong on any post that spiked early and went flat.

**2. Post times are forecasts.** `/agent/status` and `/agent/queue` project
when a draft will publish by replaying the scheduled fires against current
guardrails. A pause, a failed post or an edit moves them.

**3. Silence is not health.** Every error record has to be *sent* by a posting
machine. A desktop that is switched off produces no errors and does no work.
`/agent/status` lists when each machine last reported and flags any that has
been quiet for 6 hours; `/agent/logs` says the same thing when it finds
nothing.

---

## `problems` vs `logs`

`/agent/problems` is the interpreted view: deduplicated, severity-ranked, each
item carrying an explanation and where to fix it. It is the right default and
the wrong thing to debug with — it collapses the third identical timeout that
tells you a selector is broken rather than a page being slow.

`/agent/logs` returns what the desktop actually sent, newest first, nothing
collapsed and nothing hidden, from both `flow_errors` and failed
`post_attempts`. Use it to see *how often* something is happening.

`/agent/status` always ends with a pointer at the open problem count, so a
status report can never read "everything is fine" while Diagnostics is on fire.

---

## Adding an endpoint

Two functions in `backend/app/services/agent.py`: a `*_report` that returns a
dict and a `render_*` that turns that exact dict into prose. Then one route in
`backend/app/routers/agent.py` with a docstring — `/agent/help` picks up the
route, its docstring, and its query parameters automatically.

Keep the caveats inside the sentences. That is the whole reason the text
rendering exists.
