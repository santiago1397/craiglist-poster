"""Reads retry, claims do not.

`queue_client._request` had no retry at all. One dropped TCP connection ended
the run - fail-closed, nothing attempted until the next scheduled slot four
hours later. Two of those were observed in a single day on the production
poster, one landing exactly on the 09:00 slot. Every VPS container restart did
the same thing, because Traefik answers 404 or 502 while the API is being
recreated.

The half that matters more is what must NOT retry. `POST /claim` takes work: if
the server handled a claim and the response was lost coming back, a retry
claims a second draft while the first sits `claimed` until the stale-claim
reaper frees it. A missed read costs seconds; a duplicated claim costs a draft.

No network - `httpx.request` is replaced, so every attempt is counted rather
than sent. Run with the desktop venv:

    uv run python tests/test_queue_retry.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from craigslist_auto import queue_client as qc  # noqa: E402

ok = []
qc.RETRY_BACKOFF = (0.0, 0.0)  # keep the suite fast; the policy is tested below

import os  # noqa: E402

os.environ["QUEUE_URL"] = "https://api.example.com/queue"
os.environ["MACHINE_TOKEN"] = "1.secret"

calls = []


class FakeResponse:
    def __init__(self, status_code, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def install(responses):
    """Replace httpx.request with a scripted sequence of outcomes."""
    calls.clear()
    queue = list(responses)

    def fake(method, url, **kwargs):
        calls.append((method, url))
        outcome = queue.pop(0) if queue else queue_last[0]
        queue_last[0] = outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    queue_last = [responses[-1]]
    httpx.request = fake


OK = FakeResponse(200, {"guardrails": {"x": 1}})


# ---------------------------------------------------------------------------
# Reads recover
# ---------------------------------------------------------------------------

install([httpx.ConnectError("[WinError 10054] forcibly closed"), OK])
assert qc.fetch_settings() == {"x": 1}
assert len(calls) == 2, f"a reset connection was not retried: {len(calls)} call(s)"
ok.append("a dropped connection on a read is retried and recovers")

install([FakeResponse(502, text="Bad Gateway"), FakeResponse(502, text="Bad Gateway"), OK])
assert qc.fetch_settings() == {"x": 1}
assert len(calls) == 3
ok.append("502 during a container restart is retried and recovers")

# The exact failure that cost a posting slot: Traefik's plain-text 404 while
# the API container is being recreated.
install([FakeResponse(404, text="404 page not found"), OK])
assert qc.fetch_settings() == {"x": 1}
assert len(calls) == 2
ok.append("Traefik's 404 during a deploy is retried and recovers")

install([FakeResponse(200, {"drafts": [{"id": 1}]})])
assert qc.fetch_queue() == [{"id": 1}]
assert len(calls) == 1, "a call that succeeds first time must not be repeated"
ok.append("a successful read is made exactly once")


# ---------------------------------------------------------------------------
# Reads give up, and say why
# ---------------------------------------------------------------------------

install([httpx.ConnectError("down"), httpx.ConnectError("down"), httpx.ConnectError("down")])
try:
    qc.fetch_settings()
    raise AssertionError("a permanently unreachable server did not raise")
except qc.QueueUnavailable as e:
    assert "ConnectError" in str(e), f"the underlying cause was lost: {e}"
assert len(calls) == qc.RETRY_ATTEMPTS, f"expected {qc.RETRY_ATTEMPTS} attempts, got {len(calls)}"
ok.append(f"a read gives up after {qc.RETRY_ATTEMPTS} attempts and keeps the cause")

# 401 is settled, not transient. Retrying only delays the message that says to
# reissue the token.
install([FakeResponse(401, text="unauthorized")])
try:
    qc.fetch_settings()
    raise AssertionError("401 did not raise")
except qc.QueueUnavailable as e:
    assert "reissue" in str(e).lower()
assert len(calls) == 1, "a rejected machine token must not be retried"
ok.append("401 fails immediately and tells you to reissue the token")

install([FakeResponse(400, text="bad request")])
try:
    qc.fetch_settings()
    raise AssertionError("400 did not raise")
except qc.QueueUnavailable:
    pass
assert len(calls) == 1, "a 4xx that is not in RETRY_STATUS must not be retried"
ok.append("a non-retryable 4xx fails on the first attempt")

# A 200 carrying HTML means we reached a proxy's error page, not the API.
install([FakeResponse(200, None, text="<html>gateway</html>")])
try:
    qc.fetch_settings()
    raise AssertionError("a non-JSON 200 did not raise")
except qc.QueueUnavailable as e:
    assert "not JSON" in str(e)
ok.append("a 200 that is not JSON fails closed instead of raising a decode error")


# ---------------------------------------------------------------------------
# Claims must never be retried
# ---------------------------------------------------------------------------

install([httpx.ConnectError("lost the response"), FakeResponse(200, {"draft": {"id": 2}})])
try:
    qc.claim(["craigs1"])
    raise AssertionError("claim swallowed a transport error")
except qc.QueueUnavailable:
    pass
assert len(calls) == 1, (
    f"claim was attempted {len(calls)} times - a retried claim can take a second "
    "draft while the first stays locked"
)
ok.append("a failed claim is NOT retried (a second attempt could take another draft)")

install([FakeResponse(502, text="Bad Gateway"), FakeResponse(200, {"draft": None})])
try:
    qc.claim_edit("7811111111")
    raise AssertionError("claim_edit swallowed a 502")
except qc.QueueUnavailable:
    pass
assert len(calls) == 1, "claim_edit must not be retried either"
ok.append("a failed edit claim is NOT retried")

# The polling reads the reporter daemon does every ~15s must retry, or a deploy
# window silently drops operator "Post now" requests on the floor.
install([FakeResponse(503, text="unavailable"), FakeResponse(200, {"requests": []})])
assert qc.post_requests(["craigs1"]) == {"requests": []}
assert len(calls) == 2
ok.append("post-request polling retries, so a restart does not drop a Post now")

install([FakeResponse(503, text="unavailable"), FakeResponse(200, {"edits": []})])
assert qc.edits_pending(["craigs1"]) == {"edits": []}
assert len(calls) == 2
ok.append("edit polling retries too")


# ---------------------------------------------------------------------------
# The delay is bounded
# ---------------------------------------------------------------------------

qc.RETRY_BACKOFF = (1.0, 3.0)
install([httpx.ConnectError("down"), httpx.ConnectError("down"), httpx.ConnectError("down")])
started = time.monotonic()
try:
    qc.fetch_settings()
except qc.QueueUnavailable:
    pass
elapsed = time.monotonic() - started
# 1s + 3s of backoff. Generous ceiling so a slow machine does not fail the
# suite, tight enough to catch an unbounded or exponential-forever policy.
assert 3.5 <= elapsed <= 8.0, f"total backoff was {elapsed:.1f}s, expected ~4s"
ok.append(f"total retry delay stays bounded (~{elapsed:.1f}s), so the 15s poll loop keeps up")

# ---------------------------------------------------------------------------
# Image downloads
#
# A failed download during an edit reconcile is fatal by design -
# `fetch_post_images` refuses to publish a partial set, because the reconcile
# replaces a live posting's images wholesale - so one blip on one image can
# leave a published ad flagged `degraded_live` and waiting on a human.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import tempfile  # noqa: E402

qc.RETRY_BACKOFF = (0.0, 0.0)

BLOB = b"pretend-jpeg-bytes"
DIGEST = hashlib.sha256(BLOB).hexdigest()


class FakeBytesResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


def install_get(responses):
    """Scripted httpx.get, and a fresh cache dir so nothing is served from disk."""
    calls.clear()
    qc.IMAGE_CACHE = Path(tempfile.mkdtemp()) / "images"
    queue = list(responses)

    def fake_get(url, **kwargs):
        calls.append(("GET", url))
        return queue.pop(0) if queue else responses[-1]

    httpx.get = fake_get


install_get([FakeBytesResponse(502), FakeBytesResponse(200, BLOB)])
assert qc.fetch_image(7, DIGEST).read_bytes() == BLOB
assert len(calls) == 2, f"a 502 on an image was not retried: {len(calls)} call(s)"
ok.append("a 502 on an image download is retried and recovers")

# A truncated body fails the digest check, which is exactly how a partial
# transfer shows up. Retrying is right; accepting it would publish a broken
# image to a live posting.
install_get([FakeBytesResponse(200, b"truncated"), FakeBytesResponse(200, BLOB)])
assert qc.fetch_image(8, DIGEST).read_bytes() == BLOB
assert len(calls) == 2, "a digest mismatch was not retried"
ok.append("a truncated download fails the digest check and is retried")

install_get([FakeBytesResponse(200, b"still-wrong")])
try:
    qc.fetch_image(9, DIGEST)
    raise AssertionError("a persistently corrupt image was accepted")
except qc.QueueUnavailable as e:
    assert "digest mismatch" in str(e)
assert len(calls) == qc.RETRY_ATTEMPTS
assert not list(qc.IMAGE_CACHE.glob("*")), "a corrupt image was written to the cache"
ok.append("a persistently corrupt image is refused and never cached")

install_get([FakeBytesResponse(403)])
try:
    qc.fetch_image(10, DIGEST)
    raise AssertionError("403 did not raise")
except qc.QueueUnavailable:
    pass
assert len(calls) == 1, "a non-retryable status on an image must not be retried"
ok.append("a non-retryable status on an image fails on the first attempt")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
