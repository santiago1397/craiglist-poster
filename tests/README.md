# Tests

Plain scripts, no test framework — the project has no pytest dependency and
these are meant to be runnable anywhere with nothing installed but the app's own
requirements. Each one exits non-zero on the first failed assertion and prints a
line per check on success.

## No database needed

```bash
uv run python tests/test_events_schema.py     # event union, new fields, legacy compat
uv run python tests/test_guardrail_clamp.py   # compiled ceilings actually clamp
uv run python tests/test_outbox_guard.py     # only unsent *posted* attempts block a claim
uv run python tests/test_browser_lease.py    # two flows can't share one Chrome profile
uv run python tests/test_edit_guards.py      # content hash, edit clamps, posting-slot guard
uv run python tests/test_failure_reporting.py # every failure reaches the VPS debuggable
uv run python tests/test_queue_retry.py      # reads retry, claims never do
PYTHONPATH=backend uv run python tests/test_agent_api.py  # agent auth boundaries + the caveats in every answer
PYTHONPATH=backend uv run python tests/test_agent_tools.py # the CLI and MCP wrappers in tools/
```

`test_agent_tools.py` replaces `urlopen`, so it inspects requests instead of
sending them. The property it exists for: **the CLI must never put the key in a
URL.** The HTTP surface accepts `?key=` because many AI fetch tools cannot set
headers, and that concession writes the key into access logs — a shell has no
such limitation, so the CLI has no excuse. It also pins that a guardrail refusal
exits non-zero with its reasons intact, and reaches an MCP client as readable
tool output rather than a transport error a model would just retry.

`test_agent_api.py` covers the read surface AI agents use. Two things it pins:
the publish endpoint refuses a key in the query string **even when a valid
header is also present** (by then the secret is already in the access log), and
the prose renderers keep their caveats — that stats are a once-daily scrape,
that post times are forecasts, that no errors is not health. Those conditions
live inside sentences rather than in JSON fields precisely because a model drops
a field when summarising and does not drop a clause it is reading.

`test_failure_reporting.py` covers the rules that decide whether a failure can
be debugged at all: `failed_step` is never None, every step the poster can die
on is classified pre- or post-upload, and a degraded post keeps
`outcome='posted'` while still carrying its warnings. **Add a step to
`poster.py` and this test fails until you classify it** in
`PRE_UPLOAD_STEPS` — which is the point, because an unclassified step parks a
draft that did not need parking. See [DIAGNOSTICS.md](../DIAGNOSTICS.md).

`test_queue_retry.py` covers the half of the retry policy that is easy to get
wrong. Reads retry, because one dropped TCP connection used to end a whole
posting run — two were observed in a single day, one landing exactly on the
09:00 slot, and every VPS container restart did the same thing because Traefik
answers 404 or 502 while the API is being recreated. **Claims must never
retry**: if the server handled a claim and the response was lost coming back, a
second attempt takes a second draft while the first sits `claimed` until the
stale-claim reaper frees it. A missed read costs seconds; a duplicated claim
costs a draft.

`test_events_schema.py` covers the thing most likely to break silently: an
outbox written by an older build must still validate after a schema change, or
events queued during an upgrade are lost.

`test_guardrail_clamp.py` covers decision 14 — a server that asks for 30
posts/day must be clamped to the compiled ceiling rather than obeyed.

## Database needed

These run against a **scratch** database. Never point them at production: they
`TRUNCATE`.

```bash
createdb cl_scratch

export POSTGRES_HOST=localhost POSTGRES_PORT=5432
export POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres POSTGRES_DB=cl_scratch
export ADMIN_EMAIL=a@b.c ADMIN_PASSWORD_HASH=x
export JWT_SECRET=0123456789abcdef0123456789abcdef
export INGEST_BEARER_TOKEN=0123456789abcdef
export DISPLAY_TZ=America/New_York

cd backend && uv run alembic upgrade head && cd ..

PYTHONPATH=backend uv run python tests/test_routes_auth.py    # no DB reads, but imports the app
PYTHONPATH=backend uv run python tests/test_queue_logic.py    # eligibility, claim, state machine
PYTHONPATH=backend uv run python tests/test_accounts_list.py  # account picker on a fresh DB
PYTHONPATH=backend uv run python tests/test_queue_http.py     # machine tokens over HTTP
PYTHONPATH=backend uv run python tests/test_posting_switch.py # pause/resume actually stops claims
PYTHONPATH=backend uv run python tests/test_edit_logic.py     # desired state, staleness, decision-16-style routing
PYTHONPATH=backend uv run python tests/test_edit_images.py    # staging images on a live posting
PYTHONPATH=backend uv run python tests/test_hydration_evidence.py  # the selector census reaches the dashboard
PYTHONPATH=backend uv run python tests/test_agent_reports.py  # the agent reports' SQL, empty and populated
PYTHONPATH=backend uv run python tests/test_db_timezone.py    # date boundaries are local, not UTC

dropdb cl_scratch
```

`test_agent_reports.py` runs every agent report against an empty database and a
populated one — an empty install is the case most likely to raise and the first
one a new agent hits. It also pins the stats window bug: `snapshot_date` is
written in America/New_York while `CURRENT_DATE` is evaluated in the database's
timezone, so a `CURRENT_DATE - N` baseline selects the same row as the latest
snapshot every evening after 20:00 ET and reports zero views for every post,
with no error anywhere. The window is anchored to each post's own newest
snapshot instead. **Change that query and this test fails**, which is the point.

`test_db_timezone.py` pins something invisible until evening. `snapshot_date`
is written in America/New_York, Postgres on the VPS runs in UTC, so
`CURRENT_DATE` and `timestamptz::date` roll over four hours before the data
does. Between 20:00 and midnight Eastern, a scrape that had just finished
reported `age_days = 1`, `days_active` gained a day, and the young-post filter
excluded posts early. The pool now opens connections with
`-c timezone=<DISPLAY_TZ>`; remove that and this test fails **at any hour**,
because it compares against the wall clock rather than assuming the run happens
inside the broken window.

`test_queue_logic.py` is the important one. It exercises the parts that only
fail at runtime — the SQL, `pg_trgm`, the claim's `FOR UPDATE SKIP LOCKED`, and
the draft state machine — including the decision-16 split where a failure before
photo upload requeues but a failure after it parks for review.
