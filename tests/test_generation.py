"""Generation must never leave the queue empty.

Posting is fail-closed, so the fallback is not a nicety — if the model is down
and generation gives up, the system stops advertising. These checks assert the
fallback actually produces usable drafts, and that bad model output is caught
rather than published.
"""
from datetime import datetime, timezone

from app.db import conn, init_pool, tx
from app.services import drafts as drafts_svc
from app.services import generator

init_pool()
ok = []

SEEDS = [
    ("Broward", "Davie", "33314", "(954) 634-7420", "Roof Repair Serving Davie",
     "Quality roofing work in Davie.\n\nWe reroof.\n\nWe repair.\n\nLicensed.\n\nCall (954) 634-7420."),
    ("Miami-Dade", "Doral", "33166", "(954) 634-7370", "Roof Repair Serving Doral",
     "Quality roofing work in Doral.\n\nWe reroof.\n\nWe repair.\n\nLicensed.\n\nCall (954) 634-7370."),
    # Monroe is not routable by poster._select_subarea and must never be picked.
    ("Monroe", "Key West", "33040", "(954) 634-7360", "Roof Repair Serving Key West",
     "Quality roofing work in Key West.\n\nCall (954) 634-7360."),
]
TAIL = "roofing, roof repair, metal roofing\n\n33004 33009\n\nDavie, Doral"

with tx() as c:
    c.execute("TRUNCATE drafts, seed_ads, account_states, posts, post_attempts CASCADE")
    for county, city, zipc, phone, title, head in SEEDS:
        c.execute(
            "INSERT INTO seed_ads (county, city, postal_code, phone_number, "
            "license_number, service_offered, fallback_title, fallback_head) "
            "VALUES (%s,%s,%s,%s,'CCC1334317','skilled trade services',%s,%s)",
            (county, city, zipc, phone, title, head),
        )
    c.execute("UPDATE generation_settings SET tail_template=%s, enabled=TRUE WHERE singleton", (TAIL,))
    # topup only generates for accounts the machines have reported.
    for acct in ("craigs1", "craigs2"):
        c.execute(
            "INSERT INTO account_states (event_id, ts, machine, account, eligible_now, "
            "posts_last_24h_total, posts_last_7d_this_account) "
            "VALUES (%s, NOW(), 'm1', %s, TRUE, 0, 0)",
            (f"ev-{acct}", acct),
        )

# --- 1. No API key configured -> fallback, not failure ---------------------
with tx() as c:
    d = generator.build_draft(c, account="craigs1")
assert d is not None, "generation produced nothing without an API key"
assert d["generated_by"] == "fallback", d["generated_by"]
assert d["title"] in [s[4] for s in SEEDS]
assert d["status"] == "queued"
ok.append("no API key -> falls back to workbook copy and still queues a draft")

# --- 2. The shared tail is appended, and only once -------------------------
assert d["body"].endswith(TAIL), "tail not appended"
assert d["body"].count("roofing, roof repair") == 1, "tail duplicated"
assert d["body_head"] and TAIL not in d["body_head"], "head must not contain the tail"
ok.append("tail appended exactly once; head stays free of it")

# --- 3. Monroe seeds are never selected ------------------------------------
with tx() as c:
    picked = {generator.build_draft(c, account="craigs1")["city"] for _ in range(12)}
assert "Key West" not in picked, f"picked a non-routable Monroe seed: {picked}"
assert picked <= {"Davie", "Doral"}, picked
ok.append("Monroe seeds excluded from generation (not routable)")

# --- 4. AI path records itself distinctly ----------------------------------
real_call = generator.call_model
generator.call_model = lambda s, seed, angle: (
    f"Metal Roofing and Repair in {seed['city']} - Free Estimate",
    f"Hi neighbours in {seed['city']}.\n\nWe reroof.\n\nWe repair.\n\n"
    f"Licensed and insured.\n\nCall {seed['phone_number']}.",
)
try:
    with tx() as c:
        ai = generator.build_draft(c, account="craigs2")
    assert ai["generated_by"] == "ai", ai["generated_by"]
    assert ai["city"] in ai["title"]
    assert ai["body"].endswith(TAIL)
finally:
    generator.call_model = real_call
ok.append("model path marks drafts 'ai' and still gets the tail")

# --- 5. Bad model output is rejected, not published ------------------------
bad_cases = [
    ({"title": "x", "body_head": "y"}, "too short"),
    ({"title": "A perfectly reasonable roofing headline here", "body_head": ""}, "empty body"),
    ({"nope": 1}, "missing fields"),
    ({"title": "A perfectly reasonable roofing headline here",
      "body_head": "word, " * 200}, "keyword-list shaped"),
]
seed = {"city": "Davie", "phone_number": "(954) 634-7420"}
for payload, label in bad_cases:
    try:
        generator._validate(payload, seed)
        raise AssertionError(f"validation accepted {label} output")
    except generator.GenerationError:
        pass
# Missing phone number must also be caught.
try:
    generator._validate(
        {"title": "A perfectly reasonable roofing headline here",
         "body_head": "A" * 500}, seed)
    raise AssertionError("validation accepted output with no phone number")
except generator.GenerationError:
    pass
ok.append("validation rejects short, empty, malformed, keyword-dump and phone-less output")

# --- 6. JSON extraction survives fences and surrounding prose --------------
assert generator._extract_json('```json\n{"title":"t","body_head":"b"}\n```')["title"] == "t"
assert generator._extract_json('Sure!\n{"title":"t","body_head":"b"}\nHope that helps')["title"] == "t"
try:
    generator._extract_json("no json here")
    raise AssertionError("accepted non-JSON")
except generator.GenerationError:
    pass
ok.append("JSON extraction handles code fences and chatty prose")

# --- 6b. Real-world malformed output the model actually produces -----------
# MiniMax writes literal newlines between paragraphs instead of \n escapes,
# which is invalid JSON by the spec. In production this alone caused ~1 in 3
# generations to fall back before strict=False was used.
raw_newlines = '{"title": "Roof Repair in Davie", "body_head": "Para one.\n\nPara two.\n\nCall now."}'
got = generator._extract_json(raw_newlines)
assert got["title"] == "Roof Repair in Davie", got
assert "\n\n" in got["body_head"], "paragraph breaks lost"
ok.append("literal newlines inside JSON strings parse instead of falling back")

# An unescaped quote inside the body is salvaged rather than wasting the call.
unescaped_quote = '{"title": "Roof "Repair" in Davie", "body_head": "Body text here."}'
got = generator._extract_json(unescaped_quote)
assert got["body_head"] == "Body text here.", got
ok.append("output with an unescaped quote is salvaged")

# --- 7. topup respects floor/target and only known accounts ----------------
with tx() as c:
    c.execute("TRUNCATE drafts CASCADE")
    c.execute("UPDATE guardrail_settings SET queue_depth_floor=3, queue_depth_target=5")
    result = generator.topup(c)
assert result["created"] == 10, f"expected 5 per account x2, got {result['created']}"
assert set(result["accounts"]) == {"craigs1", "craigs2"}
with conn() as c:
    for acct in ("craigs1", "craigs2"):
        n = drafts_svc.list_drafts(c, account=acct, status="queued")["total"]
        assert n == 5, f"{acct} has {n} drafts, expected 5"
ok.append("topup fills each known account to target (5), no others")

# --- 7b. A small batch limit is shared, not eaten by the first account -----
with tx() as c:
    c.execute("TRUNCATE drafts CASCADE")
    shared = generator.topup(c, limit=4)
assert shared["created"] == 4, shared
per_account = {a: v["created"] for a, v in shared["accounts"].items() if v["created"]}
assert len(per_account) == 2, f"one account consumed the whole batch: {per_account}"
assert all(n == 2 for n in per_account.values()), per_account
ok.append("a batch limit smaller than the shortfall is split evenly, not drained by one account")

# --- 8. Above the floor, topup does nothing --------------------------------
# 7b deliberately left the queues below the floor, so refill first.
with tx() as c:
    generator.topup(c)
with tx() as c:
    again = generator.topup(c)
assert again["created"] == 0, f"topup generated {again['created']} while above floor"
ok.append("topup is a no-op while every queue is above the floor")

# --- 9. Disabling generation stops it --------------------------------------
with tx() as c:
    c.execute("TRUNCATE drafts CASCADE")
    c.execute("UPDATE generation_settings SET enabled=FALSE WHERE singleton")
    off = generator.topup(c)
assert off["created"] == 0 and "disabled" in off.get("skipped", ""), off
with tx() as c:
    forced = generator.topup(c, force=True, limit=2)
assert forced["created"] == 2, forced
    # restore
with tx() as c:
    c.execute("UPDATE generation_settings SET enabled=TRUE WHERE singleton")
ok.append("disabled generation is skipped; force overrides it, respecting limit")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
