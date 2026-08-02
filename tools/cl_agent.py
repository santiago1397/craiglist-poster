#!/usr/bin/env python3
"""Command-line access to the Craigslist poster's agent API.

Standard library only, single file, no install. Copy it anywhere Python 3.9+
runs and it works — which is the point. A tool that needs `pip install` first is
a tool an agent gives up on.

    export CL_AGENT_KEY=<key from Settings -> API keys>
    ./cl_agent.py status
    ./cl_agent.py stats --window 7d
    ./cl_agent.py post-now 123

Reading needs any key. Composing — `locations`, `generate-image`,
`approve-image`, `draft-*` — needs an 'agent'-scope key. Note what composing is
not: a draft written here is UNREVIEWED, nothing in this tool can change that,
and an unreviewed draft cannot publish. A person decides whether the words go
out on a live listing.

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


def _read_json_arg(value: str) -> dict:
    """A draft body from a file, from `-` for stdin, or inline.

    Ad copy is thousands of characters with newlines in it. Passing that as a
    shell flag is how you end up with a draft whose body was mangled by quoting
    — so the normal path is a file or a pipe, and inline JSON is the fallback.
    """
    raw = sys.stdin.read() if value == "-" else None
    if raw is None:
        try:
            with open(value, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            # Not a path — assume it is the JSON itself.
            raw = value
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise Failure(
            f"Could not parse that as JSON: {e}\n"
            "Pass a path to a .json file, '-' to read stdin, or inline JSON."
        ) from None
    if not isinstance(parsed, dict):
        raise Failure("The draft must be a JSON object, not a list or a scalar.")
    return parsed


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
    # --- compose: needs an 'agent'-scope key -------------------------------
    "locations": lambda a: _request("locations", {"format": _fmt(a)}),
    "generate-image": lambda a: _request(
        "images/generate", {}, method="POST",
        body={"prompt": a.prompt, "kind": a.kind, "count": a.count, "city": a.city},
    ),
    "approve-image": lambda a: _request(
        f"images/{a.image_id}/approve", {}, method="POST", body={}
    ),
    "draft-create": lambda a: _request(
        "drafts", {}, method="POST", body=_read_json_arg(a.draft)
    ),
    "draft-show": lambda a: _request(f"drafts/{a.draft_id}", {}),
    "draft-patch": lambda a: _request(
        f"drafts/{a.draft_id}", {}, method="PATCH", body=_read_json_arg(a.changes)
    ),
    "draft-cover": lambda a: _request(
        f"drafts/{a.draft_id}/cover", {}, method="POST",
        body={"image_id": a.image_id},
    ),
    "draft-autofill": lambda a: _request(
        f"drafts/{a.draft_id}/autofill", {}, method="POST", body={"count": a.count}
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

    # --- compose ----------------------------------------------------------
    # Everything below needs an 'agent'-scope key. None of it can publish: a
    # draft written here is unreviewed, and only a human in the dashboard can
    # change that.

    add("locations", "Where an ad may be placed, and which places are already used.")

    gen = add("generate-image", "Generate an image. Costs money; lands unapproved.")
    gen.add_argument("--prompt", help="What to draw. Omit for the stored prompt.")
    gen.add_argument("--kind", default="photo", choices=["photo", "cover"],
                     help="cover = slot 1 thumbnail; photo = slots 2-24")
    gen.add_argument("--count", type=int, default=1)
    gen.add_argument("--city", help="interpolated into the stored prompt")

    approve = add("approve-image", "Approve an image THIS key generated.")
    approve.add_argument("image_id", type=int)

    create = add("draft-create", "Write a new draft. It is created unreviewed.")
    create.add_argument(
        "draft",
        help="path to a .json file, '-' for stdin, or inline JSON. See "
             "`cl_agent.py help` for the fields.",
    )

    show = add("draft-show", "Read one draft back, with images and similarity.")
    show.add_argument("draft_id", type=int)

    patch = add("draft-patch", "Change a draft this key created.")
    patch.add_argument("draft_id", type=int)
    patch.add_argument("changes", help="path to a .json file, '-' for stdin, or inline JSON")

    cover = add("draft-cover", "Put an approved cover image in slot 1.")
    cover.add_argument("draft_id", type=int)
    cover.add_argument("image_id", type=int)

    fill = add("draft-autofill", "Fill the draft's empty photo slots (2-24).")
    fill.add_argument("draft_id", type=int)
    fill.add_argument("--count", type=int, default=23)

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
