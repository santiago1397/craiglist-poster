"""The CLI and MCP wrappers in tools/.

Both are thin, and the thin part is not what breaks. What breaks is a wrapper
that quietly disagrees with the API it wraps — a flag that maps to the wrong
query parameter, a refusal reported as success, or a key that ends up somewhere
it was specifically designed not to go.

The property worth naming: **the CLI must never put the key in a URL.** The HTTP
surface accepts `?key=` because many AI fetch tools cannot set headers, and that
concession writes the key into access logs. A shell can set headers, so the CLI
has no reason to use the leaky path, and this test fails if it ever starts.

No network and no database — `urllib.request.urlopen` is replaced, so every
request is inspected rather than sent.
"""
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import cl_agent  # noqa: E402
import cl_agent_mcp  # noqa: E402

ok = []
os.environ["CL_AGENT_KEY"] = "42.testsecret"
os.environ["CL_AGENT_URL"] = "https://api.example.com"

sent = []


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(request, timeout=None):
    sent.append(request)
    return _FakeResponse(b"OK")


urllib.request.urlopen = fake_urlopen


def run(argv):
    """Invoke the CLI and hand back the request it would have sent.

    stdout is captured so the fake response body does not interleave with this
    script's own pass/fail lines.
    """
    sent.clear()
    real_stdout, sys.stdout = sys.stdout, io.StringIO()
    try:
        code = cl_agent.main(argv)
    finally:
        sys.stdout = real_stdout
    assert code == 0, f"{argv} exited {code}"
    return sent[-1]


# ---------------------------------------------------------------------------
# The key never travels in a URL
# ---------------------------------------------------------------------------

DRAFT_JSON = json.dumps({
    "account": "craigs1", "title": "t", "body": "b",
    "county": "Broward", "city": "Davie",
})

ALL_COMMANDS = (
    ["status"], ["queue"], ["posts"], ["stats"], ["problems"], ["logs"],
    ["inventory"], ["help"], ["post-now", "7"],
    # Compose. These carry an 'agent'-scope key in practice, which is the one
    # kind the server refuses in a query string on *every* verb — so a leak
    # here is not a rotation, it is a key that can publish sitting in a log.
    ["locations"], ["generate-image", "--prompt", "a roof"], ["approve-image", "12"],
    ["draft-create", DRAFT_JSON], ["draft-show", "5"],
    ["draft-patch", "5", '{"title": "x"}'],
    ["draft-cover", "5", "12"], ["draft-autofill", "5"],
)

for argv in ALL_COMMANDS:
    request = run(argv)
    assert "key=" not in request.full_url, \
        f"{argv} put the key in the URL: {request.full_url}"
    assert "testsecret" not in request.full_url, f"{argv} leaked the secret into the URL"
    assert request.get_header("X-api-key") == "42.testsecret", \
        f"{argv} did not send the key as a header"
ok.append(
    f"cli OK (all {len(ALL_COMMANDS)} commands send the key as a header, never in the URL)"
)

# ---------------------------------------------------------------------------
# Flags reach the API as the parameters it actually documents
# ---------------------------------------------------------------------------

request = run(["stats", "--window", "30d", "--account", "craigs2", "--limit", "5"])
assert "window=30d" in request.full_url and "account=craigs2" in request.full_url
assert "limit=5" in request.full_url
assert request.full_url.startswith("https://api.example.com/agent/stats?")
ok.append("cli OK (stats flags map to window/account/limit on /agent/stats)")

request = run(["queue", "--reviewed"])
assert "reviewed=true" in request.full_url, request.full_url
# Absent, not `reviewed=false` — the API treats the parameter as a tri-state
# where unset means "both", and false would silently hide reviewed drafts.
request = run(["queue"])
assert "reviewed" not in request.full_url, request.full_url
ok.append("cli OK (--reviewed is a tri-state: set means true, unset means unfiltered)")

request = run(["logs", "--hours", "48", "--flow", "post"])
assert "hours=48" in request.full_url and "flow=post" in request.full_url
ok.append("cli OK (logs filters are passed through)")

request = run(["inventory", "--json"])
assert "format=json" in request.full_url
request = run(["inventory"])
assert "format" not in request.full_url, "prose is the default; format must be absent"
ok.append("cli OK (--json opts into JSON; prose stays the default)")

request = run(["post-now", "123"])
assert request.get_method() == "POST", "post-now must be a POST"
assert json.loads(request.data.decode()) == {"draft_id": 123}
assert "?" not in request.full_url, "post-now must send nothing in the query string"
ok.append("cli OK (post-now POSTs the draft id in a body, with an empty query string)")

# ---------------------------------------------------------------------------
# Compose commands
# ---------------------------------------------------------------------------

request = run(["generate-image", "--prompt", "a tile roof", "--kind", "cover", "--count", "2"])
assert request.get_method() == "POST"
assert json.loads(request.data.decode()) == {
    "prompt": "a tile roof", "kind": "cover", "count": 2, "city": None,
}
ok.append("cli OK (generate-image sends prompt, kind and count in the body)")

request = run(["approve-image", "77"])
assert request.full_url.endswith("/agent/images/77/approve")
assert request.get_method() == "POST"
ok.append("cli OK (approve-image targets the image it names)")

# The body is thousands of characters with newlines in it, so it has to arrive
# through a file or a pipe intact — not through shell quoting.
request = run(["draft-create", DRAFT_JSON])
assert json.loads(request.data.decode())["city"] == "Davie"
ok.append("cli OK (draft-create accepts inline JSON)")

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_draft_tmp.json")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({"account": "craigs2", "title": "from a file",
                             "body": "line one\nline two", "county": "Broward",
                             "city": "Davie"}))
try:
    request = run(["draft-create", path])
    body = json.loads(request.data.decode())
    assert body["title"] == "from a file"
    assert body["body"] == "line one\nline two", "newlines did not survive the file path"
finally:
    os.remove(path)
ok.append("cli OK (draft-create reads a file, and newlines in the copy survive)")

request = run(["draft-cover", "9", "12"])
assert request.full_url.endswith("/agent/drafts/9/cover")
assert json.loads(request.data.decode()) == {"image_id": 12}
ok.append("cli OK (draft-cover names the draft in the path and the image in the body)")

request = run(["draft-autofill", "9", "--count", "10"])
assert json.loads(request.data.decode()) == {"count": 10}
ok.append("cli OK (draft-autofill passes the slot count through)")

# Malformed JSON must fail before a request is sent, not after — a half-built
# draft on the server is worse than a refusal here.
sent.clear()
err = io.StringIO()
real_stderr, sys.stderr = sys.stderr, err
try:
    code = cl_agent.main(["draft-create", "{not json"])
finally:
    sys.stderr = real_stderr
assert code == 1, "malformed JSON should exit non-zero"
assert not sent, "a request was sent despite the body failing to parse"
assert "JSON" in err.getvalue()
ok.append("cli OK (unparseable draft JSON fails before anything is sent)")

# ---------------------------------------------------------------------------
# A refusal is never reported as success
# ---------------------------------------------------------------------------

def failing_urlopen(request, timeout=None):
    raise urllib.error.HTTPError(
        request.full_url, 409, "Conflict", {},
        io.BytesIO(json.dumps({"detail": {
            "message": "craigs1 cannot post right now",
            "reasons": ["weekend: posting restricted to Mon-Fri"],
            "advice": "This is a guardrail, not a transient error. Do not retry.",
        }}).encode()),
    )


urllib.request.urlopen = failing_urlopen
err = io.StringIO()
real_stderr, sys.stderr = sys.stderr, err
try:
    code = cl_agent.main(["post-now", "5"])
finally:
    sys.stderr = real_stderr

assert code == 1, f"a refused post exited {code}, expected 1"
text = err.getvalue()
assert "craigs1 cannot post right now" in text
assert "weekend: posting restricted to Mon-Fri" in text, "the guardrail reason was dropped"
assert "Do not retry" in text, "the do-not-retry instruction was dropped"
ok.append("cli OK (a refused post exits non-zero and relays the guardrail reasons verbatim)")

urllib.request.urlopen = fake_urlopen

# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

replies = []
cl_agent_mcp._send = lambda message: replies.append(message)


def call(message):
    replies.clear()
    cl_agent_mcp.handle(message)
    return replies


out = call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
assert out[0]["result"]["protocolVersion"] == cl_agent_mcp.PROTOCOL_VERSION
assert "tools" in out[0]["result"]["capabilities"]
ok.append("mcp OK (initialize advertises the tools capability)")

# A notification has no id and must never be answered. Some hosts treat a
# response to one as a fatal protocol violation.
assert call({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []
ok.append("mcp OK (notifications are not answered)")

out = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = out[0]["result"]["tools"]
assert len(tools) == len(cl_agent_mcp.TOOLS), "tools/list dropped a tool"
for tool in tools:
    assert set(tool) == {"name", "description", "inputSchema"}, \
        f"{tool['name']} exposes unexpected keys: {sorted(tool)}"
    assert tool["inputSchema"]["type"] == "object"
    # The description is the only thing guaranteed to be read before a tool is
    # chosen, so an empty one is a broken tool.
    assert len(tool["description"]) > 80, f"{tool['name']} is under-described"
ok.append(f"mcp OK ({len(tools)} tools, each with a schema and a description worth reading)")

publish = next(t for t in tools if t["name"] == "craigslist_post_now")
assert publish["inputSchema"]["required"] == ["draft_id"]
lowered = publish["description"].lower()
for warning in ("cannot be undone", "reviewed", "do not retry", "confirm with the user"):
    assert warning in lowered, f"the publish tool does not warn about: {warning}"
ok.append("mcp OK (the one destructive tool says so, and says to confirm first)")

# The compose tools are new and each carries a caveat a model would otherwise
# get wrong. These are asserted individually because the failure mode is
# specific: a model that believes creating a draft published it will tell the
# user their ad is live when it is sitting in a review queue.
compose_warnings = {
    "craigslist_generate_image": ("spends real money", "unapproved"),
    "craigslist_approve_image": ("only on images generated by this same key",),
    "craigslist_create_draft": ("unreviewed", "cannot publish"),
    "craigslist_locations": ("closed",),
    "craigslist_autofill_photos": ("not an error",),
}
by_name = {t["name"]: t for t in tools}
for name, warnings in compose_warnings.items():
    assert name in by_name, f"{name} is missing from tools/list"
    lowered = by_name[name]["description"].lower()
    for warning in warnings:
        assert warning in lowered, f"{name} does not warn about: {warning}"
ok.append("mcp OK (compose tools carry their caveats: spend, approval, the review gate)")

# The property the whole compose surface rests on. A model reading only tool
# descriptions must not come away thinking it can publish what it wrote.
assert "cannot publish" in by_name["craigslist_create_draft"]["description"].lower()
assert "mark it reviewed" in by_name["craigslist_create_draft"]["description"].lower() \
    or "marks it reviewed" in by_name["craigslist_create_draft"]["description"].lower()
ok.append("mcp OK (creating a draft is described as composing, never as posting)")

out = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "craigslist_status", "arguments": {}}})
assert out[0]["result"]["content"][0]["text"] == "OK"
assert not out[0]["result"].get("isError")
ok.append("mcp OK (a tool call returns text content)")

out = call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "does_not_exist", "arguments": {}}})
assert out[0]["result"]["isError"] is True
ok.append("mcp OK (an unknown tool is an error result, not a crash)")

out = call({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"})
assert out[0]["error"]["code"] == -32601
ok.append("mcp OK (an unknown method returns JSON-RPC -32601)")

# A guardrail refusal must arrive as tool *output*, not a transport error — a
# model retries a failed call and relays a failed result.
urllib.request.urlopen = failing_urlopen
out = call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "craigslist_post_now", "arguments": {"draft_id": 1}}})
assert "error" not in out[0], "a refusal was reported as a JSON-RPC transport error"
assert out[0]["result"]["isError"] is True
assert "weekend" in out[0]["result"]["content"][0]["text"]
ok.append("mcp OK (a guardrail refusal is readable tool output, not a transport error)")

# Every MCP tool must point at a real endpoint on the server. This is the check
# that catches a tool renamed on one side only — the kind of break that reaches
# a user as an unexplained 404 mid-task.
#
# Arguments cover every id any tool takes, so one dict drives them all. Paths
# now carry ids (`/images/12/approve`), so the live route table's placeholders
# are turned into patterns rather than compared literally.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
urllib.request.urlopen = fake_urlopen
try:
    from app.routers.agent import router  # noqa: E402

    live = [
        re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path.lstrip("/")) + "$")
        for r in router.routes if hasattr(r, "path")
    ]
    args = {"draft_id": 1, "image_id": 2, "account": "craigs1", "title": "t",
            "body": "b", "county": "Broward", "city": "Davie"}
    for tool in cl_agent_mcp.TOOLS:
        sent.clear()
        tool["call"](args)
        path = sent[-1].full_url.split("/agent/")[1].split("?")[0]
        assert any(p.match(path) for p in live), \
            f"{tool['name']} calls /agent/{path}, which does not exist"
    ok.append(f"mcp OK (all {len(cl_agent_mcp.TOOLS)} tools target endpoints that exist)")
except ImportError as e:
    ok.append(f"mcp SKIP (backend not importable here: {e})")

print("\n".join(ok))
