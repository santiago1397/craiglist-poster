"""Import the FastAPI app, assert phase-1 routes exist and are actually guarded."""
from fastapi.testclient import TestClient

from app.main import create_app

app = create_app()
paths = set(app.openapi()["paths"])

required = [
    "/queue", "/queue/claim", "/queue/settings", "/queue/eligibility",
    "/drafts", "/drafts/health", "/drafts/{draft_id}",
    "/drafts/{draft_id}/reorder", "/drafts/{draft_id}/requeue",
    "/settings/guardrails", "/settings/machine-tokens",
]
missing = [r for r in required if r not in paths]
assert not missing, f"missing routes: {missing}"
print(f"routes OK ({len(required)} new endpoints registered)")

client = TestClient(app, raise_server_exceptions=False)

# Auth must reject before anything touches the database — there is no DB here,
# so a 401 proves the guard runs first. A 500 would mean the guard is missing.
checks = [
    ("POST", "/queue/claim", {"accounts": ["craigs1"]}),
    ("GET", "/queue", None),
    ("GET", "/queue/settings", None),
    ("GET", "/drafts", None),
    ("PUT", "/settings/guardrails", {"max_posts_per_day_total": 3}),
    ("DELETE", "/drafts/1", None),
]
for method, path, body in checks:
    r = client.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} returned {r.status_code}, expected 401"
print(f"auth OK ({len(checks)} endpoints reject unauthenticated callers with 401)")

# A machine token must not open the admin surface, and vice versa.
r = client.get("/drafts", headers={"Authorization": "Bearer 1.somesecret"})
assert r.status_code == 401, f"/drafts accepted a bearer token: {r.status_code}"
print("auth OK (machine token does not unlock the admin surface)")

# Malformed machine tokens must be rejected on shape, before any DB lookup.
for bad in ["Bearer notanid.secret", "Bearer 1", "Bearer ", "Token 1.x"]:
    r = client.post("/queue/claim", json={"accounts": ["c"]}, headers={"Authorization": bad})
    assert r.status_code == 401, f"malformed token {bad!r} returned {r.status_code}"
print("auth OK (malformed machine tokens rejected without a DB round-trip)")

# The ingest surface must still work the way it did before this change.
r = client.post("/events/batch", json={"events": []})
assert r.status_code == 401, f"/events/batch no longer requires a token: {r.status_code}"
print("regression OK (/events still guarded by the ingest token)")

print("ALL CHECKS PASSED")
