"""Craigslist's real body limit, which is not the one it advertises.

Learned from a live posting run: a body of 15,945 characters by
`effective_body_length` was rejected as "longer than 16000", and 15,412
published. Craigslist counts a textarea's CRLF line breaks and the HTML-escaped
value, so `len()` under-reports by roughly the margin a naive check would have
left. Two generated drafts sat in the queue failing every scheduled slot before
anything caught it.

The URL half of the same incident is in tests/test_post_url.py, which needs the
desktop venv rather than this one.

Backend test — run with the backend venv:
    PYTHONPATH=backend uv run python tests/test_body_limit.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.drafts import (  # noqa: E402
    POSTING_BODY_LIMIT,
    check_body_length,
    effective_body_length,
)
from app.services.generator import _fit_body  # noqa: E402

ok = []


def check(label, cond):
    if not cond:
        print(f"  FAIL  {label}")
        raise SystemExit(1)
    ok.append(label)


print("\nbody length is measured the way Craigslist measures it")
check("plain ascii counts as itself", effective_body_length("hello") == 5)
check("a bare newline costs two", effective_body_length("a\nb") == 4)
check("an existing CRLF is not double counted", effective_body_length("a\r\nb") == 4)
check("an ampersand costs five", effective_body_length("Repair & Replace") == 20)
check("None is empty, not an error", effective_body_length(None) == 0)

# The specific regression: a body len() calls safe that Craigslist refuses.
# 14,980 raw characters, but 40 bare newlines each cost two on the wire.
naive_safe = "x" * 14_940 + "\n" * 40
check(
    "len() under-reports a body that would be rejected",
    len(naive_safe) < POSTING_BODY_LIMIT < effective_body_length(naive_safe),
)

print("\nan over-length body is refused before it can reach a posting slot")
try:
    check_body_length("x" * (POSTING_BODY_LIMIT + 1))
    raise AssertionError("an over-length body was accepted")
except ValueError as e:
    check("refused with a count to act on", "shorten it" in str(e).lower())

check_body_length("x" * (POSTING_BODY_LIMIT - 1))
ok.append("a body under the limit passes")

print("\nthe generator cannot produce a body Craigslist will reject")
head = "Real ad copy that a human reads. " * 20
tail = ", ".join(f"keyword{i} phrase" for i in range(3000))
fitted = _fit_body(head, tail)
check("an oversized tail is trimmed to fit", effective_body_length(fitted) <= POSTING_BODY_LIMIT)
check("the ad copy survives the trim intact", fitted.startswith(head))
check("the trim lands on a clean boundary", not fitted.rstrip().endswith(","))
check(
    "a tail that already fits is left byte-exact",
    _fit_body("head", "small tail") == "head\n\nsmall tail",
)
check("no tail means no separator", _fit_body("head", "") == "head")
check(
    "the trimmed body would itself pass the create/edit guard",
    check_body_length(fitted) is None,
)

print("")
for line in ok:
    print(f"  OK  {line}")
print(f"\n{len(ok)} checks passed")
