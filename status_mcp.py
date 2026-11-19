#!/usr/bin/env python3
"""jobs status-card server (workbench plan, phase 2): one MCP tool, report_status.

Claude Code starts this as a stdio MCP server, registered at user level. The
session it reports for is the one whose pid is this process's parent
(sessions/<pid>.json), so the tool takes no session id. The card lands in
cards/<sessionId>.json, which the collector merges into the task (DESIGN.md §3).

Stdlib only, Python 3.9 syntax, run with `python3 -I status_mcp.py`. Messages are
newline-delimited JSON-RPC on stdin/stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROTOCOL = "2024-11-05"
BLOCKED_ON = ("you", "limit", "ci", "deploy", "task", "other")
MAX_STEPS = 20
MAX_TEXT = 300
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

TOOL = {
    "name": "report_status",
    "description": (
        "Report this session's status to the jobs dashboard. Call it at the start with the goal "
        "and the plan as steps, again at each step (update done), and whenever you get blocked. "
        "blocked is null when you're not blocked, otherwise {on, why}: on is 'you' when you're "
        "waiting for the person, 'limit' for a usage or rate limit, 'ci', 'deploy', 'task' "
        "(another task) or 'other'."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "The goal, in one line."},
            "steps": {
                "type": "array",
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}},
                    "required": ["text", "done"],
                },
            },
            "now": {"type": "string", "description": "What you are doing right now, in one line."},
            "blocked": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "on": {"type": "string", "enum": list(BLOCKED_ON)},
                            "why": {"type": "string"},
                        },
                        "required": ["on"],
                    },
                ],
            },
        },
    },
}


class CardError(ValueError):
    """The arguments don't make a card. The message goes back to the model."""


def _text(v: Any, field: str) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise CardError(f"{field} must be a string")
    return v[:MAX_TEXT]


def validate(args: Any) -> Dict[str, Any]:
    if not isinstance(args, dict):
        raise CardError("arguments must be an object")
    steps: List[Dict[str, Any]] = []
    raw_steps = args.get("steps")
    if raw_steps is not None:
        if not isinstance(raw_steps, list):
            raise CardError("steps must be a list")
        if len(raw_steps) > MAX_STEPS:
            raise CardError(f"at most {MAX_STEPS} steps")
        for i, s in enumerate(raw_steps):
            if not (isinstance(s, dict) and isinstance(s.get("text"), str)
                    and isinstance(s.get("done"), bool)):
                raise CardError(f"steps[{i}] needs text (string) and done (boolean)")
            steps.append({"text": s["text"][:MAX_TEXT], "done": s["done"]})
    blocked = args.get("blocked")
    if blocked is not None:
        if not isinstance(blocked, dict) or blocked.get("on") not in BLOCKED_ON:
            raise CardError(f"blocked must be null or {{on, why}} with on one of {', '.join(BLOCKED_ON)}")
        blocked = {"on": blocked["on"], "why": _text(blocked.get("why"), "blocked.why")}
    return {
        "goal": _text(args.get("goal"), "goal"),
        "now": _text(args.get("now"), "now"),
        "steps": steps if raw_steps is not None else None,
        "blocked": blocked,
    }


def session_for_parent(sessions_dir: Path, ppid: int) -> str:
    """The session id of the Claude Code process that started this server."""
    try:
        d = json.loads((sessions_dir / f"{ppid}.json").read_bytes())
    except (OSError, ValueError) as e:
        raise CardError(f"no session file for this server's parent process ({e.__class__.__name__})")
    if not isinstance(d, dict):
        raise CardError("the session file for this server's parent process isn't an object")
    sid = d.get("sessionId")
    if d.get("pid") != ppid or not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise CardError("the session file for this server's parent process doesn't match")
    return sid


def write_card(cards_dir: Path, sid: str, card: Dict[str, Any], now_ms: int) -> None:
    """Atomic and private, like the collector's own files."""
    path = cards_dir / f"{sid}.json"
    tmp = cards_dir / f".{sid}.{os.getpid()}.tmp"
    body = dict(card, sessionId=sid, updatedAt=now_ms)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Context:
    def __init__(self, sessions_dir: Path, cards_dir: Path, ppid: int,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self.sessions_dir = sessions_dir
        self.cards_dir = cards_dir
        self.ppid = ppid
        self.clock = clock


def call_tool(params: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    if params.get("name") != TOOL["name"]:
        raise CardError(f"unknown tool {params.get('name')!r}")
    card = validate(params.get("arguments") or {})
    sid = session_for_parent(ctx.sessions_dir, ctx.ppid)
    write_card(ctx.cards_dir, sid, card, ctx.clock())
    return {"content": [{"type": "text", "text": "Status recorded."}], "isError": False}


def handle(msg: Dict[str, Any], ctx: Context) -> Optional[Dict[str, Any]]:
    """The JSON-RPC response for one message, or None for a notification."""
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        return None  # notifications such as notifications/initialized
    if method == "initialize":
        result: Any = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "jobs-status", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        try:
            result = call_tool(msg.get("params") or {}, ctx)
        except CardError as e:
            print(f"status_mcp: report refused: {e}", file=sys.stderr, flush=True)
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv: Optional[List[str]] = None) -> int:
    home = Path(os.path.expanduser("~"))
    ctx = Context(
        sessions_dir=home / ".claude" / "sessions",
        cards_dir=Path(os.environ.get("JOBS_CARDS", str(home / "jobs" / "cards"))),
        ppid=os.getppid(),
    )
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print("status_mcp: not a JSON-RPC message", file=sys.stderr, flush=True)
            continue
        if not isinstance(msg, dict):
            continue
        try:
            resp = handle(msg, ctx)
        except OSError as e:
            print(f"status_mcp: cannot write the card: {e.strerror}", file=sys.stderr, flush=True)
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "result": {"content": [{"type": "text", "text": "The card could not be written."}],
                               "isError": True}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
