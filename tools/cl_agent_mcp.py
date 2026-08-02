#!/usr/bin/env python3
"""MCP server exposing the Craigslist poster's agent API as tools.

Standard library only — MCP over stdio is JSON-RPC 2.0 with three methods that
matter, and implementing them directly costs less than asking anyone to install
an SDK into whatever environment the host happens to run this in.

Register it with an MCP-capable host, e.g. `.mcp.json`:

    {
      "mcpServers": {
        "craigslist": {
          "command": "python",
          "args": ["tools/cl_agent_mcp.py"],
          "env": {"CL_AGENT_KEY": "<key from Settings -> API keys>"}
        }
      }
    }

Where this beats the CLI: the tools arrive already named, described and typed,
so the model never builds a URL or parses `--help`. Where it loses: it only
works in a host that speaks MCP, which is why the CLI and the plain HTTP surface
both still exist. All three call the identical endpoints.

Tool descriptions below are written for a model that has never seen this system.
They say what the tool answers *and* what would mislead someone reading the
answer, because the description is the only thing guaranteed to be read before
the tool is chosen.
"""
from __future__ import annotations

import json
import os
import sys

# The host launches this by absolute path from an arbitrary working directory,
# so `cl_agent` is only importable if its directory is on the path explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cl_agent import Failure, _request  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"

_ACCOUNT = {"type": "string", "description": "Restrict to one account, e.g. craigs1."}
_LIMIT = {"type": "integer", "description": "How many rows to return."}

TOOLS = [
    {
        "name": "craigslist_status",
        "description": (
            "Current state of the Craigslist auto-poster: whether each account "
            "may post right now and the exact reason if not, when the next post "
            "is due, how many drafts are queued and reviewed, and when each "
            "posting machine last checked in. Start here for 'how are we doing'. "
            "Post times are forecasts that move if anything fails or is paused. "
            "Also reports the count of open problems, so a quiet answer here is "
            "never the same as a healthy system."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "call": lambda a: _request("status", {}),
    },
    {
        "name": "craigslist_queue",
        "description": (
            "Ads written and waiting to publish. Shows which have been reviewed "
            "by a human, which are missing a cover image or photos, and roughly "
            "when each will go out. Only reviewed drafts can be published via "
            "craigslist_post_now."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": _ACCOUNT,
                "reviewed": {"type": "boolean", "description": "Only human-approved drafts."},
                "limit": _LIMIT,
            },
        },
        "call": lambda a: _request("queue", {
            "account": a.get("account"), "limit": a.get("limit"),
            "reviewed": "true" if a.get("reviewed") else None,
        }),
    },
    {
        "name": "craigslist_posts",
        "description": (
            "Ads that have already published: whether each is still live, "
            "whether it is ghosted (invisible in public search), and whether an "
            "edit is staged or has gone wrong. Liveness comes from a once-daily "
            "scrape, so it can lag by a day."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": _ACCOUNT,
                "since": {"type": "string", "description": "Window: 7d, 30d, 90d or all."},
                "limit": _LIMIT,
            },
        },
        "call": lambda a: _request("posts", {
            "account": a.get("account"), "since": a.get("since"), "limit": a.get("limit"),
        }),
    },
    {
        "name": "craigslist_stats",
        "description": (
            "How many views and impressions the ads earned during a period. "
            "Impressions = times an ad appeared in a results page; views = "
            "clicks into the full posting. These are real period figures, "
            "computed as the difference between daily snapshots — not lifetime "
            "averages. The underlying scrape runs once a day at 06:00 Eastern on "
            "the posting desktop, so today is never complete and a day that "
            "machine was off leaves a gap."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "enum": ["yesterday", "7d", "30d"],
                    "description": "Period to measure.",
                },
                "account": _ACCOUNT,
                "limit": _LIMIT,
            },
        },
        "call": lambda a: _request("stats", {
            "window": a.get("window", "7d"), "account": a.get("account"),
            "limit": a.get("limit"),
        }),
    },
    {
        "name": "craigslist_problems",
        "description": (
            "Everything currently wrong, ranked by severity, each with a "
            "plain-English explanation of what it means and where to fix it. "
            "Use this before concluding the system is healthy — a machine that "
            "has silently stopped reporting shows up here and nowhere else."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "How far back to look."},
                "limit": _LIMIT,
            },
        },
        "call": lambda a: _request("problems", {
            "hours": a.get("hours"), "limit": a.get("limit"),
        }),
    },
    {
        "name": "craigslist_logs",
        "description": (
            "Raw error records exactly as the posting desktop reported them, "
            "newest first, nothing deduplicated. Use this rather than "
            "craigslist_problems when you need to know how OFTEN something is "
            "failing — three identical timeouts mean a broken selector, one "
            "means a slow page. An empty result does not prove health: these "
            "records have to be sent by a machine, and a machine that is off "
            "sends nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "How far back to look."},
                "account": _ACCOUNT,
                "flow": {"type": "string", "description": "e.g. post, edit, stats_sync."},
                "limit": _LIMIT,
            },
        },
        "call": lambda a: _request("logs", {
            "hours": a.get("hours"), "account": a.get("account"),
            "flow": a.get("flow"), "limit": a.get("limit"),
        }),
    },
    {
        "name": "craigslist_inventory",
        "description": (
            "Whether there are enough images to fill the queued ads. Each post "
            "takes one cover image (the Craigslist thumbnail) and up to 23 "
            "photos, from two separate stacks. Being short does not block "
            "posting; the ads just publish with fewer images."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "call": lambda a: _request("inventory", {}),
    },
    {
        "name": "craigslist_post_now",
        "description": (
            "Publish one draft immediately instead of waiting for its scheduled "
            "slot. THIS PUBLISHES A REAL PUBLIC ADVERT and cannot be undone once "
            "the desktop picks it up, roughly 15 seconds later. It consumes one "
            "of only three posting slots per day. It works only on drafts a "
            "human has already marked reviewed, and only with a 'post'-scope "
            "key. If the guardrails refuse, the reasons come back verbatim — "
            "report them to the user and do not retry, because a guardrail is a "
            "rule, not a transient error. Confirm with the user before calling "
            "this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "integer",
                    "description": "id of a queued, reviewed draft (see craigslist_queue).",
                },
            },
            "required": ["draft_id"],
        },
        "call": lambda a: _request(
            "post-now", {}, method="POST", body={"draft_id": a["draft_id"]}
        ),
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(request_id, payload: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")

    # Notifications carry no id and must never be answered — a response to one
    # is a protocol violation that some hosts treat as fatal.
    if request_id is None:
        return

    if method == "initialize":
        _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "craigslist-agent", "version": "1.0.0"},
        })
    elif method == "tools/list":
        _result(request_id, {
            "tools": [
                {k: t[k] for k in ("name", "description", "inputSchema")}
                for t in TOOLS
            ]
        })
    elif method == "tools/call":
        params = message.get("params") or {}
        tool = BY_NAME.get(params.get("name"))
        if tool is None:
            _result(request_id, {
                "content": [{"type": "text", "text": f"No such tool: {params.get('name')}"}],
                "isError": True,
            })
            return
        try:
            text = tool["call"](params.get("arguments") or {})
            _result(request_id, {"content": [{"type": "text", "text": text}]})
        except Failure as e:
            # Reported as tool output rather than a JSON-RPC error: the model
            # needs to read a guardrail refusal and relay it, not see the call
            # fail with a code it will simply retry.
            _result(request_id, {
                "content": [{"type": "text", "text": str(e)}], "isError": True,
            })
        except Exception as e:  # never take the server down over one call
            _result(request_id, {
                "content": [{"type": "text", "text": f"Unexpected failure: {e!r}"}],
                "isError": True,
            })
    else:
        _send({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
