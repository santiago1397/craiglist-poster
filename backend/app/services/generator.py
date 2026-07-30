"""Draft generation — AI copy with a workbook fallback.

The contract that matters: **the queue must keep filling**. Posting is
fail-closed, so an empty queue means zero ads. If the model is unreachable,
rate-limited, out of credit, or returns something unusable, this falls back to
the seed row's own workbook copy and carries on. A bad night at the API costs
you fresh wording, never a posting slot.

Only the head is generated. The ~14,000-character keyword tail is identical on
every ad and is appended from generation_settings.tail_template, which keeps it
byte-exact and off the token bill.

Seeds in counties the poster cannot route (Monroe — see app.reference) are
excluded, so generation cannot manufacture ads that would file themselves under
the wrong Craigslist subarea.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone

import httpx
import psycopg
from loguru import logger

from ..config import get_settings
from ..reference import subarea_supported
from . import drafts as drafts_svc

REQUEST_TIMEOUT = 60.0

# Steers the opening paragraph so a batch does not read as one voice repeated.
ANGLES = [
    "", "storm damage", "financing available", "leak repair",
    "roof replacement", "free inspection", "insurance claim help",
    "tile and metal roofing", "emergency tarping", "hurricane season prep",
]

DEFAULT_SYSTEM_PROMPT = """\
You write Craigslist "skilled trade services" ads for a licensed, insured
South Florida roofing contractor. Return ONLY valid JSON.

HARD RULES
- Plain, direct American English, 6th-8th grade reading level.
- Never invent credentials, awards, years in business, warranties, prices,
  guarantees, or specific past jobs.
- Never claim cheapest / best / #1 / top-rated.
- No emoji. No ALL-CAPS words.
- The phone number appears exactly once, in the final sentence.
- Do NOT write keyword lists, zip lists, or city lists. Those are appended
  automatically and must not appear in your output.

OUTPUT
{"title": string, "body_head": string}

TITLE
- 50-70 characters. Contains the service and the city.
- Vary the phrasing each time. Do not reuse one template.

BODY_HEAD - exactly 5 paragraphs, separated by one blank line:
1. Hook, 1-2 sentences. What a {city} homeowner gets. May reference local
   building codes or storm season where it reads naturally.
2. Core offering, 1 sentence: full reroofing and new roof installation,
   gutter repair and installation, attic insulation.
3. Services, 1 sentence: emergency and temporary repairs, leak detection and
   interior water damage, shingle / tile / metal / flat roof work, flashing,
   wood rot, fascia and soffit.
4. Trust, 1 sentence: fully licensed and insured, experienced with local
   homeowners, financing available, references on request.
5. Call to action, 1 sentence, ending with the phone number exactly as given.
"""

DEFAULT_USER_TEMPLATE = """\
city: {city}
county: {county}
zip: {zip_code}
service: {service}
phone: {phone}
license: {license}
angle: {angle}
"""


class GenerationError(RuntimeError):
    """Model call failed or returned something unusable. Always recoverable —
    the caller falls back to workbook copy."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_generation_settings(conn: psycopg.Connection) -> dict:
    row = conn.execute("SELECT * FROM generation_settings LIMIT 1").fetchone()
    if row is None:  # pragma: no cover — migration seeds it
        raise RuntimeError("generation_settings is empty; run migrations")
    g = dict(row)
    # Blank prompts fall back to the built-in defaults so a fresh install
    # generates sensibly before anyone edits anything.
    g["system_prompt"] = (g.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT
    g["user_template"] = (g.get("user_template") or "").strip() or DEFAULT_USER_TEMPLATE
    return g


def update_generation_settings(conn: psycopg.Connection, values: dict) -> dict:
    allowed = {
        "enabled", "model", "api_base", "temperature",
        "system_prompt", "user_template", "tail_template",
    }
    patch = {k: v for k, v in values.items() if k in allowed and v is not None}
    if patch:
        sets = ", ".join(f"{k} = %({k})s" for k in patch)
        conn.execute(
            f"UPDATE generation_settings SET {sets}, updated_at = NOW() WHERE singleton",
            patch,
        )
    return get_generation_settings(conn)


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _extract_json(content: str) -> dict:
    """Models wrap JSON in prose or fences often enough to be worth handling."""
    text = content.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise GenerationError(f"no JSON object in model output: {content[:200]!r}")
        text = text[start: end + 1]
    # strict=False permits literal control characters inside strings. The model
    # writes real newlines between paragraphs rather than \n escapes, which is
    # invalid JSON by the letter of the spec — in production this alone caused
    # roughly a third of generations to fall back.
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError:
        data = _salvage(text)
    if not isinstance(data, dict):
        raise GenerationError("model returned JSON that is not an object")
    return data


# Last-resort extraction for output that is JSON-shaped but not parseable —
# typically an unescaped quote inside the body. Cheaper than burning a
# regeneration, and anything it returns still has to pass _validate.
_TITLE_RE = re.compile(r'"title"\s*:\s*"(.*?)"\s*,\s*"body_head"', re.S)
_HEAD_RE = re.compile(r'"body_head"\s*:\s*"(.*)"\s*\}?\s*$', re.S)


def _salvage(text: str) -> dict:
    title_m, head_m = _TITLE_RE.search(text), _HEAD_RE.search(text)
    if not (title_m and head_m):
        raise GenerationError(
            f"model output was not valid JSON and could not be salvaged: {text[:200]!r}"
        )
    unescape = lambda s: s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    logger.debug("salvaged title/body_head from malformed JSON")
    return {"title": unescape(title_m.group(1)), "body_head": unescape(head_m.group(1))}


def _validate(data: dict, seed: dict) -> tuple[str, str]:
    title = str(data.get("title") or "").strip()
    head = str(data.get("body_head") or data.get("body") or "").strip()

    if not title or not head:
        raise GenerationError("model output missing title or body_head")
    if not (20 <= len(title) <= 120):
        raise GenerationError(f"title length {len(title)} outside 20-120")
    if not (200 <= len(head) <= 4000):
        raise GenerationError(f"body_head length {len(head)} outside 200-4000")
    # The prompt forbids keyword dumps; a comma-storm means it ignored that and
    # the tail would be duplicated.
    if head.count(",") > 60:
        raise GenerationError("body_head looks like a keyword list")
    if seed["phone_number"] and seed["phone_number"] not in head:
        raise GenerationError("body_head does not contain the phone number")
    return title, head


def call_model(settings_row: dict, seed: dict, angle: str) -> tuple[str, str]:
    """Ask the model for a title + head. Raises GenerationError on any problem."""
    api_key = get_settings().minimax_api_key
    if not api_key:
        raise GenerationError("MINIMAX_API_KEY is not configured")

    user = settings_row["user_template"].format(
        city=seed["city"],
        county=seed["county"],
        zip_code=seed["postal_code"],
        service=seed["service_offered"],
        phone=seed["phone_number"],
        license=seed["license_number"],
        angle=angle or "none",
    )
    payload = {
        "model": settings_row["model"],
        "messages": [
            {"role": "system", "content": settings_row["system_prompt"]},
            {"role": "user", "content": user},
        ],
        "temperature": settings_row["temperature"],
    }
    url = settings_row["api_base"].rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise GenerationError(f"model request failed: {e!r}") from e
    if resp.status_code // 100 != 2:
        raise GenerationError(f"model HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise GenerationError(f"unexpected model response shape: {e}") from e

    return _validate(_extract_json(content), seed)


# ---------------------------------------------------------------------------
# Seed selection
# ---------------------------------------------------------------------------

def pick_seed(conn: psycopg.Connection, rng: random.Random) -> dict | None:
    """Least-recently-used routable seed, so the pool rotates evenly."""
    rows = conn.execute(
        """
        SELECT s.*, COALESCE(u.n, 0) AS times_used
        FROM seed_ads s
        LEFT JOIN (SELECT seed_ad_id, COUNT(*) n FROM drafts GROUP BY seed_ad_id) u
               ON u.seed_ad_id = s.id
        WHERE s.active
        ORDER BY times_used, random()
        LIMIT 40
        """
    ).fetchall()
    candidates = [dict(r) for r in rows if subarea_supported(r["county"])]
    if not candidates:
        return None
    fewest = candidates[0]["times_used"]
    return rng.choice([c for c in candidates if c["times_used"] == fewest])


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_draft(
    conn: psycopg.Connection, *, account: str, rng: random.Random | None = None
) -> dict | None:
    """Create one queued draft. Returns the draft, or None if no seed exists."""
    rng = rng or random.Random()
    g = get_generation_settings(conn)
    seed = pick_seed(conn, rng)
    if seed is None:
        logger.warning("no routable seed ads available; cannot generate")
        return None

    angle = rng.choice(ANGLES)
    try:
        title, head = call_model(g, seed, angle)
        source = "ai"
    except GenerationError as e:
        # This is the designed path, not an outage. Log it, record it, continue.
        logger.warning(f"generation fell back to workbook copy: {e}")
        title, head = seed["fallback_title"], seed["fallback_head"]
        source = "fallback"
        conn.execute(
            "UPDATE generation_settings SET last_error = %s, "
            "fallback_total = fallback_total + 1 WHERE singleton",
            (str(e)[:500],),
        )
    else:
        conn.execute(
            "UPDATE generation_settings SET last_error = NULL, "
            "generated_total = generated_total + 1 WHERE singleton"
        )

    tail = g.get("tail_template") or ""
    body = f"{head}\n\n{tail}" if tail else head

    draft = drafts_svc.create_draft(conn, {
        "account": account,
        "title": title,
        "body": body,
        "body_head": head,
        "county": seed["county"],
        "city": seed["city"],
        # Defaults to the city; widen it per draft in the composer to catch
        # searches from neighbouring towns.
        "geographic_area": seed["city"],
        "service_offered": seed["service_offered"],
        "postal_code": seed["postal_code"],
        "license_number": seed["license_number"],
        "phone_number": seed["phone_number"],
        "source": f"generated:{source}",
    })
    conn.execute(
        "UPDATE drafts SET seed_ad_id = %s, generated_by = %s WHERE id = %s",
        (seed["id"], source, draft["id"]),
    )
    conn.execute(
        "UPDATE generation_settings SET last_run_at = NOW(), last_source = %s "
        "WHERE singleton",
        (source,),
    )

    # Best-effort: images are optional, so an empty stack yields a text-only
    # post rather than a failed draft.
    from . import images as images_svc

    draft["images"] = images_svc.autoattach(
        conn, draft_id=draft["id"], account=account, rng=rng
    )
    draft["generated_by"] = source
    return draft


def known_accounts(conn: psycopg.Connection) -> list[str]:
    """Accounts the machines have actually reported. Avoids inventing names."""
    rows = conn.execute(
        "SELECT DISTINCT account FROM account_states "
        "WHERE account <> '(none)' ORDER BY account"
    ).fetchall()
    return [r["account"] for r in rows]


# Every caller — the background loop and the manual endpoint — must serialise
# through this. Without it they race: a run that starts while another is still
# committing reads a stale depth of 0 and generates a second full batch, which
# is exactly how a target of 15 produced 18 per account in production.
TOPUP_LOCK_KEY = 8_412_337_001


def topup(conn: psycopg.Connection, *, force: bool = False, limit: int = 50) -> dict:
    """Bring every account's queue up to target. Returns a summary.

    Runs on a floor/target hysteresis so it generates in useful batches rather
    than one draft at a time: nothing happens until an account drops below the
    floor, then it fills to target.

    Serialised by a transaction-scoped advisory lock. A concurrent caller does
    not queue up behind it — it returns immediately, because by the time the
    holder finishes the queue is full and there is nothing left to do.
    """
    if not conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s) AS ok", (TOPUP_LOCK_KEY,)
    ).fetchone()["ok"]:
        logger.info("topup already running elsewhere; skipping this run")
        return {"skipped": "already running", "created": 0, "accounts": {}}

    g = get_generation_settings(conn)
    if not g["enabled"] and not force:
        return {"skipped": "generation disabled", "created": 0, "accounts": {}}

    guard = conn.execute("SELECT * FROM guardrail_settings LIMIT 1").fetchone()
    floor, target = guard["queue_depth_floor"], guard["queue_depth_target"]

    accounts = known_accounts(conn)
    if not accounts:
        return {"skipped": "no accounts known yet", "created": 0, "accounts": {}}

    rng = random.Random()
    summary: dict[str, dict] = {}
    wants: dict[str, int] = {}

    for account in accounts:
        depth = conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE account = %s AND status = 'queued'",
            (account,),
        ).fetchone()["n"]
        summary[account] = {"depth": depth, "created": 0, "ai": 0, "fallback": 0}
        if depth >= floor and not force:
            summary[account]["reason"] = "above floor"
            continue
        wants[account] = max(0, target - depth)

    # Round-robin rather than draining one account at a time: with a batch limit
    # smaller than the total shortfall, the first account would otherwise eat the
    # whole budget and leave the others empty.
    created_total = 0
    while created_total < limit and any(v > 0 for v in wants.values()):
        for account, remaining in list(wants.items()):
            if remaining <= 0 or created_total >= limit:
                continue
            # Re-read depth each time rather than trusting the count taken at
            # the top of the run: generating a batch takes minutes of model
            # calls, and drafts can be added or removed meanwhile.
            live_depth = conn.execute(
                "SELECT COUNT(*) AS n FROM drafts WHERE account = %s AND status = 'queued'",
                (account,),
            ).fetchone()["n"]
            if live_depth >= target:
                wants[account] = 0
                continue

            draft = build_draft(conn, account=account, rng=rng)
            if draft is None:
                wants[account] = 0
                continue
            wants[account] -= 1
            summary[account]["created"] += 1
            summary[account]["depth"] += 1
            summary[account][draft["generated_by"]] += 1
            created_total += 1

    logger.info(f"topup created {created_total} draft(s): {summary}")
    return {
        "created": created_total,
        "accounts": summary,
        "ran_at": datetime.now(timezone.utc),
    }
