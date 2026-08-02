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

**Settings → API keys → Create key.** The key is shown once. Two scopes:

| Scope | What it opens |
|---|---|
| **Read only** | Every `/agent/*` GET. Cannot change anything. |
| **Read + publish** | The above, plus `POST /agent/post-now` for drafts already marked reviewed. |

Revoke either from the same page. `last used` shows whether a key is still
live, so a forgotten one is visible rather than silently valid forever.

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

Responses are plain English by default. Add `&format=json` when something needs
to parse one — both render from the same data, so they cannot disagree.

### What it cannot do

It cannot write ad copy, create drafts, edit live listings, change guardrails,
generate images, or spend money. Those stay in the dashboard. The only write is
`post-now`, and it is fenced:

- The draft must already be marked **reviewed** by a human. An agent can decide
  *which approved draft goes next*, never *what the ad says*.
- Every guardrail is evaluated server-side exactly as for a scheduled 9am fire —
  posting window, weekday rule, 3-per-24h cap, 7-per-account-per-week cap,
  20-hour cooldown. A refusal comes back as a 409 with the reasons verbatim and
  an instruction not to retry, because a guardrail is not a transient error.
- The request is attributed as `agent:<key label>`, so a post that went out
  off-cadence can be explained later.

It publishes to a live classifieds site under a real licence number, and one of
only three posting slots a day. That is why the review gate is not optional.

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
when a draft will publish by replaying the 9am/1pm/5pm fires against current
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
