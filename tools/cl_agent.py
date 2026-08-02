#!/usr/bin/env python3
"""Command-line access to the Craigslist poster's agent API.

Standard library only, single file, no install. Copy it anywhere Python 3.9+
runs and it works — which is the point. A tool that needs `pip install` first is
a tool an agent gives up on.

    export CL_AGENT_KEY=<key from Settings -> API keys>
    ./cl_agent.py status
    ./cl_agent.py stats --window 7d
    ./cl_agent.py post-now 123

**This sends the key in a header, always.** The HTTP surface also accepts
`?key=` because many AI fetch tools cannot set headers at all — but a shell can,
so there is no reason to use the leaky path here. Nothing this script does puts
the key in a URL, a server access log, or your shell history.

Run `./cl_agent.py help` for the server's own manual, which is generated from
its live route table and therefore cannot be out of date the way this docstring
can.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://api.craigslist.santiagoproperties.uk"
TIMEOUT_SECONDS = 30


class Failure(Exception):
    """Anything the user needs to read and act on, rather than a traceback."""


def _base_url() -> str:
    return os.environ.get("CL_AGENT_URL", DEFAULT_URL).rstrip("/")


def _key() -> str:
    key = os.environ.get("CL_AGENT_KEY", "").strip()
    if not key:
        raise Failure(
            "No API key. Create one in the dashboard under Settings -> API keys,\n"
            "then:  export CL_AGENT_KEY=<key>\n"
            "Use a 'post'-scope key if you intend to publish."
        )
    return key


def _request(path: str, params: dict, *, method: str = "GET", body: dict | None = None) -> str:
    url = f"{_base_url()}/agent/{path}"
    query = {k: v for k, v in params.items() if v is not None}
    if query:
        url += "?" + urllib.parse.urlencode(query)

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    # Header, never the query string. See the module docstring.
    request.add_header("X-API-Key", _key())
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise Failure(_explain_http_error(e)) from None
    except urllib.error.URLError as e:
        raise Failure(
            f"Could not reach {_base_url()}: {e.reason}\n"
            "Check the network, or override the host with CL_AGENT_URL."
        ) from None


def _explain_http_error(e: urllib.error.HTTPError) -> str:
    """Surface the server's own wording — it is written to be acted on.

    The API answers a refused post with the exact guardrail reasons the
    dashboard shows. Replacing that with "HTTP 409" would throw away the only
    part anybody needs.
    """
    raw = e.read().decode("utf-8", errors="replace")
    detail: object = raw
    try:
        parsed = json.loads(raw)
        detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
    except (ValueError, AttributeError):
        pass

    if isinstance(detail, dict):
        lines = [str(detail.get("message", "Request refused."))]
        lines += [f"  - {r}" for r in detail.get("reasons", [])]
        if detail.get("advice"):
            lines.append(str(detail["advice"]))
        return "\n".join(lines)

    if e.code == 401:
        return (
            f"{detail}\n\nThe key in CL_AGENT_KEY was rejected. It may have been "
            "revoked — check Settings -> API keys."
        )
    return str(detail)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _fmt(args) -> str | None:
    return "json" if getattr(args, "json", False) else None


COMMANDS = {
    "help": lambda a: _request("help", {}),
    "status": lambda a: _request("status", {"format": _fmt(a)}),
    "queue": lambda a: _request("queue", {
        "account": a.account, "limit": a.limit,
        "reviewed": "true" if a.reviewed else None, "format": _fmt(a),
    }),
    "posts": lambda a: _request("posts", {
        "account": a.account, "since": a.since, "limit": a.limit, "format": _fmt(a),
    }),
    "stats": lambda a: _request("stats", {
        "window": a.window, "account": a.account, "limit": a.limit, "format": _fmt(a),
    }),
    "problems": lambda a: _request("problems", {
        "hours": a.hours, "limit": a.limit, "format": _fmt(a),
    }),
    "logs": lambda a: _request("logs", {
        "hours": a.hours, "account": a.account, "flow": a.flow,
        "limit": a.limit, "format": _fmt(a),
    }),
    "inventory": lambda a: _request("inventory", {"format": _fmt(a)}),
    "post-now": lambda a: _request(
        "post-now", {}, method="POST", body={"draft_id": a.draft_id}
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cl_agent.py",
        description=(
            "Read the Craigslist auto-poster. Reads CL_AGENT_KEY for the key and "
            "CL_AGENT_URL for the host. Start with `help` for the server's own "
            "manual."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if name != "help":
            sub.add_argument("--json", action="store_true",
                             help="return JSON instead of prose")
        return sub

    add("help", "Print the server's manual: every question you can ask.")
    add("status", "Can each account post, when is the next post, are machines alive.")

    queue = add("queue", "Drafts waiting to publish and what each is missing.")
    queue.add_argument("--account", help="restrict to one account")
    queue.add_argument("--reviewed", action="store_true",
                       help="only drafts a human has approved")
    queue.add_argument("--limit", type=int, default=20)

    posts = add("posts", "Ads that have published, and whether they are still live.")
    posts.add_argument("--account", help="restrict to one account")
    posts.add_argument("--since", default="30d", help="e.g. 7d, 30d, 90d, all")
    posts.add_argument("--limit", type=int, default=20)

    stats = add("stats", "Views and impressions earned during a period.")
    stats.add_argument("--window", default="7d",
                       choices=["yesterday", "7d", "30d"])
    stats.add_argument("--account", help="restrict to one account")
    stats.add_argument("--limit", type=int, default=20)

    problems = add("problems", "What is wrong, ranked and explained.")
    problems.add_argument("--hours", type=int, default=72)
    problems.add_argument("--limit", type=int, default=30)

    logs = add("logs", "Raw error records, newest first, undeduplicated.")
    logs.add_argument("--hours", type=int, default=24)
    logs.add_argument("--account", help="restrict to one account")
    logs.add_argument("--flow", help="e.g. post, edit, stats_sync")
    logs.add_argument("--limit", type=int, default=40)

    add("inventory", "Whether there are enough images to fill the queue.")

    post_now = add(
        "post-now",
        "Publish a draft a human has already reviewed. Needs a 'post'-scope key.",
    )
    post_now.add_argument("draft_id", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    # The server's prose contains em-dashes and other non-ASCII. Python on
    # Windows defaults stdout to the console's legacy codepage, which raises
    # UnicodeEncodeError on the very first line of a status report. Force UTF-8
    # rather than stripping the characters out of the API's wording.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # already wrapped, or not a real stream (tests capture it)

    args = build_parser().parse_args(argv)
    try:
        sys.stdout.write(COMMANDS[args.command](args).rstrip("\n") + "\n")
    except Failure as e:
        # Non-zero and on stderr, so a script or agent that checks either one
        # notices. A refusal printed to stdout as if it were an answer is how a
        # caller ends up reporting "posted" when nothing was.
        sys.stderr.write(f"{e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
