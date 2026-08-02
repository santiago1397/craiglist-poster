"""Full HTTP round trip: issue a machine token, claim a draft through the API."""
from fastapi.testclient import TestClient

from app.db import init_pool, tx
from app.main import create_app
from app.security import issue_machine_token, revoke_machine_token
from app.services import drafts as drafts_svc

init_pool()
ok = []

with tx() as c:
    c.execute("TRUNCATE drafts, posts, post_attempts, machine_tokens CASCADE")
    # This test claims a draft, so every gate that stops a claim has to be open
    # — and the gates are about *when you run the suite*, not about anything
    # this test is checking.
    #
    # The hour window was widened first, because at the 08-19 default the test
    # passed during the working day and failed every evening. The weekday rule
    # is the same bug one axis over: it passed Monday to Friday and failed all
    # weekend. Both make the suite report a broken queue when the only thing
    # that changed is the clock, which is worse than no test — it trains you to
    # ignore a red run.
    #
    # The previous values are captured rather than assumed, and restored in a
    # `finally` rather than at the end of the happy path. `guardrail_settings`
    # is a singleton these scripts share, so a widened window used to leak into
    # whatever ran next whenever an assertion failed — turning one failure into
    # a confusing second one somewhere unrelated.
    saved = c.execute(
        "SELECT post_window_start_hour, post_window_end_hour, post_weekdays_only "
        "FROM guardrail_settings WHERE singleton"
    ).fetchone()
    c.execute(
        "UPDATE guardrail_settings SET post_window_start_hour = 0, "
        "post_window_end_hour = 24, post_weekdays_only = FALSE WHERE singleton"
    )
    d = drafts_svc.create_draft(c, {
        "account": "craigs1", "title": "Roof Repair Serving Davie",
        "body": "full body", "body_head": "full body",
    })


def restore_guardrails():
    with tx() as c:
        c.execute(
            "UPDATE guardrail_settings SET post_window_start_hour = %s, "
            "post_window_end_hour = %s, post_weekdays_only = %s WHERE singleton",
            (saved["post_window_start_hour"], saved["post_window_end_hour"],
             saved["post_weekdays_only"]),
        )


try:

    token = issue_machine_token("desktop-eseva3c", "test")
    assert token.count(".") >= 1 and token.split(".")[0].isdigit(), token
    ok.append("machine token issued in <id>.<secret> form")

    client = TestClient(create_app(), raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {token}"}

    r = client.get("/queue/settings", headers=auth)
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["machine"] == "desktop-eseva3c"
    assert r.json()["guardrails"]["max_posts_per_day_total"] >= 1
    ok.append("GET /queue/settings authenticates and returns guardrails")

    # An outbox backlog must refuse the claim before any draft is handed out.
    r = client.post("/queue/claim", headers=auth,
                    json={"accounts": ["craigs1"], "outbox_pending": 5})
    assert r.status_code == 200 and r.json()["draft"] is None
    assert r.json()["refused"] == "outbox_backlog", r.json()
    ok.append("claim refused while the desktop still holds unsent events")

    r = client.post("/queue/claim", headers=auth,
                    json={"accounts": ["craigs1"], "outbox_pending": 0})
    assert r.status_code == 200, (r.status_code, r.text)
    claimed = r.json()["draft"]
    assert claimed is not None and claimed["id"] == d["id"], r.json()
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by_machine"] == "desktop-eseva3c"
    assert claimed["attempts"] == 1, f"attempts not incremented: {claimed['attempts']}"
    assert claimed["body"] == "full body", "claim must return the full body to post"
    ok.append("claim returns the full draft, marked claimed to this machine")

    # Claiming again finds nothing — the only draft is now held.
    r = client.post("/queue/claim", headers=auth, json={"accounts": ["craigs1"]})
    assert r.json()["draft"] is None
    ok.append("queue is empty once its only draft is claimed")

    # The prefetch window must not include a claimed draft.
    r = client.get("/queue", headers=auth)
    assert r.status_code == 200 and r.json()["drafts"] == [], r.json()
    ok.append("prefetch window excludes claimed drafts")

    # Revocation must take effect immediately.
    token_id = int(token.split(".")[0])
    assert revoke_machine_token(token_id)
    r = client.get("/queue/settings", headers=auth)
    assert r.status_code == 401, f"revoked token still works: {r.status_code}"
    ok.append("revoking a machine token locks it out immediately")

    # A token for one machine must not be forgeable by swapping the id.
    bad = f"999999.{token.split('.', 1)[1]}"
    r = client.get("/queue/settings", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401
    ok.append("unknown token id is rejected")
finally:
    restore_guardrails()

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
