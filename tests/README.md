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
```

`test_failure_reporting.py` covers the rules that decide whether a failure can
be debugged at all: `failed_step` is never None, every step the poster can die
on is classified pre- or post-upload, and a degraded post keeps
`outcome='posted'` while still carrying its warnings. **Add a step to
`poster.py` and this test fails until you classify it** in
`PRE_UPLOAD_STEPS` — which is the point, because an unclassified step parks a
draft that did not need parking. See [DIAGNOSTICS.md](../DIAGNOSTICS.md).

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

dropdb cl_scratch
```

`test_queue_logic.py` is the important one. It exercises the parts that only
fail at runtime — the SQL, `pg_trgm`, the claim's `FOR UPDATE SKIP LOCKED`, and
the draft state machine — including the decision-16 split where a failure before
photo upload requeues but a failure after it parks for review.
