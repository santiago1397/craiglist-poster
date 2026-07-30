"""The Review page needs an account list before anything has ever posted."""
from fastapi.testclient import TestClient

from app.db import init_pool, tx
from app.main import create_app
from app.auth import require_admin
from app.services import drafts as drafts_svc

init_pool()
app = create_app()
# The account list itself is not the thing under test here — the SQL is — so
# stub the cookie check rather than minting a real session.
app.dependency_overrides[require_admin] = lambda: "admin@test"
client = TestClient(app, raise_server_exceptions=False)

with tx() as c:
    c.execute("TRUNCATE drafts, posts, post_attempts, account_states CASCADE")

r = client.get("/accounts")
assert r.status_code == 200, (r.status_code, r.text)
assert r.json()["accounts"] == [], r.json()
print("  OK  empty database returns an empty account list without erroring")

with tx() as c:
    drafts_svc.create_draft(c, {"account": "craigs2", "title": "t", "body": "b", "body_head": "b"})

r = client.get("/accounts")
assert r.json()["accounts"] == ["craigs2"], r.json()
print("  OK  an account appears as soon as it has a queued draft, before it has posted")

# The sentinel account the poster emits when nothing is eligible must not leak
# into the picker.
with tx() as c:
    c.execute(
        "INSERT INTO post_attempts (event_id, ts, machine, account, outcome) "
        "VALUES ('e1', NOW(), 'm', '(none)', 'skipped_no_drafts')"
    )
r = client.get("/accounts")
assert "(none)" not in r.json()["accounts"], r.json()
print("  OK  the '(none)' sentinel is excluded")
print("\n3 checks passed")
