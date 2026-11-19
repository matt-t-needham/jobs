#!/usr/bin/env python3
"""Permission prompts for workbench sessions (workbench plan, phase 3).

Claude Code starts this as an MCP server (--permission-prompt-tool) for each
broker-driven session. For each tool call that needs approval it asks the broker
over the unix socket, and the broker blocks until the visitor decides on the web or
the request times out. Any failure to reach the broker is a deny, never an allow.

Stdlib only. Run by the broker as `python3 -I approval.py --socket <path> --ticket <t>`.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any, Dict, Optional

PROTOCOL = "2024-11-05"
TOOL = {
    "name": "approve",
    "description": "Ask the person in the workbench whether this tool call may run.",
    "inputSchema": {"type": "object"},
}
# The broker times a request out at its own approval_timeout (600 s); give it longer.
SOCKET_TIMEOUT = 660.0


def ask_broker(socket_path: str, ticket: str, request: Dict[str, Any]) -> Optional[bool]:
    """True/False from the broker, or None when the broker couldn't answer."""
    body = dict(request, action="approval_request", ticket=ticket)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(SOCKET_TIMEOUT)
            s.connect(socket_path)
            s.sendall((json.dumps(body) + "\n").encode())
            line = s.makefile("rb").readline()
    except OSError as e:
        print(f"approval: broker unreachable: {e.__class__.__name__}", file=sys.stderr, flush=True)
        return None
    try:
        resp = json.loads(line)
    except ValueError:
        return None
    if not isinstance(resp, dict) or not resp.get("ok"):
        return None
    return resp.get("allow") is True


def decide(params: Dict[str, Any], socket_path: str, ticket: str) -> Dict[str, Any]:
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
    allow = ask_broker(socket_path, ticket, {"tool_name": args.get("tool_name"), "input": tool_input})
    if allow:
        verdict = {"behavior": "allow", "updatedInput": tool_input}
    else:
        verdict = {"behavior": "deny", "message": "Not approved in the workbench."}
    return {"content": [{"type": "text", "text": json.dumps(verdict)}], "isError": False}


def handle(msg: Dict[str, Any], socket_path: str, ticket: str) -> Optional[Dict[str, Any]]:
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        return None
    if method == "initialize":
        result: Any = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "jobs-approval", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = decide(msg.get("params") or {}, socket_path, ticket)
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--ticket", required=True)
    args = ap.parse_args(argv)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        resp = handle(msg, args.socket, args.ticket)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
