#!/usr/bin/env python3
"""Checks a running broker's front door (workbench plan, phase 3), like
agent-sandbox-check.py does for the agent runner. Run as matt on the host:

    python3 -I broker/probe.py [--state ~/jobs/broker]

Each check prints PASS or FAIL. Exit 0 only if every check passes. It sends no
work to Claude: every request is one that must be refused.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple


def ask(path: Path, req: dict) -> Optional[dict]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(path))
            s.sendall((json.dumps(req) + "\n").encode())
            line = s.makefile("rb").readline()
        return json.loads(line)
    except (OSError, ValueError):
        return None


def check_mode(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return False, f"{path} is missing"
    st = path.stat()
    private = stat.S_IMODE(st.st_mode) & 0o077 == 0 and st.st_uid == os.getuid()
    return private, f"{path.name} is mode {oct(stat.S_IMODE(st.st_mode))}, owner uid {st.st_uid}"


def check_refused_without_token(web: Path) -> Tuple[bool, str]:
    r = ask(web, {"action": "events", "sessionId": "00000000-0000-0000-0000-000000000000", "after": 0})
    ok = r is not None and r.get("ok") is False and r.get("status") == 403
    return ok, f"unauthenticated events -> {r and r.get('status')}"


def check_no_approval_on_web(web: Path) -> Tuple[bool, str]:
    r = ask(web, {"action": "approval_request", "ticket": "x", "tool_name": "Bash", "input": {}})
    ok = r is not None and r.get("status") == 404
    return ok, f"approval_request on the web socket -> {r and r.get('status')}"


def check_no_web_on_approval(approvals: Path) -> Tuple[bool, str]:
    r = ask(approvals, {"action": "events", "sessionId": "00000000-0000-0000-0000-000000000000",
                        "after": 0, "token": "x"})
    ok = r is not None and r.get("status") == 404
    return ok, f"events on the approval socket -> {r and r.get('status')}"


def check_no_tcp_listener() -> Tuple[bool, str]:
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"cannot list listeners: {e.__class__.__name__}"
    bad = [line for line in out.splitlines() if "broker.py" in line]
    return not bad, "no TCP listener from the broker" if not bad else f"broker listens on TCP: {bad[0]}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=Path, default=Path(os.path.expanduser("~/jobs/broker")))
    args = ap.parse_args(argv)
    web, approvals = args.state / "broker.sock", args.state / "approval.sock"
    checks: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
        ("web socket is private", lambda: check_mode(web)),
        ("approval socket is private", lambda: check_mode(approvals)),
        ("web socket refuses a request without an Access token", lambda: check_refused_without_token(web)),
        ("web socket never takes an approval request", lambda: check_no_approval_on_web(web)),
        ("approval socket takes nothing else", lambda: check_no_web_on_approval(approvals)),
        ("no TCP listener", check_no_tcp_listener),
    ]
    failed = 0
    for name, fn in checks:
        ok, detail = fn()
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    print("all checks passed" if not failed else f"{failed} check(s) FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
