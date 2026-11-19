#!/usr/bin/env python3
"""Claude Code hook → jobs spool.

Installed as a user-level hook for SessionStart, UserPromptSubmit, Notification,
PostToolUse, Stop and SessionEnd:

    python3 -I /path/to/jobs/hook.py --spool ~/jobs/spool

Reads the hook's JSON on stdin and writes one small JSON file per event into the
spool, which the collector reads and deletes. Only the event name, session id,
time, and for Notification its type and message are kept: never the prompt,
tool input or transcript path.

Exit 1 on failure: a non-blocking hook error, so Claude Code carries on and
shows the message, rather than the event silently going missing.
Standard library only, Python 3.9 syntax.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

MESSAGE_LIMIT = 300


def record(hook_input: dict, now_ms: int) -> dict:
    event = hook_input.get("hook_event_name")
    sid = hook_input.get("session_id")
    if not isinstance(event, str) or not isinstance(sid, str) or not sid:
        raise ValueError("hook input lacks hook_event_name or session_id")
    rec = {"event": event, "sessionId": sid, "at": now_ms}
    if event == "Notification":
        ntype = hook_input.get("notification_type")
        if isinstance(ntype, str):
            rec["notificationType"] = ntype
        msg = hook_input.get("message")
        if isinstance(msg, str):
            rec["message"] = msg[:MESSAGE_LIMIT]
    return rec


def write(spool: Path, rec: dict) -> Path:
    name = f"{rec['at']:015d}-{os.getpid()}"
    tmp = spool / f".{name}.tmp"
    final = spool / f"{name}.json"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, final)  # the collector only reads *.json, so never a partial file
    return final


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", required=True)
    args = ap.parse_args(argv)
    try:
        rec = record(json.load(sys.stdin), int(time.time() * 1000))
        write(Path(os.path.expanduser(args.spool)), rec)
    except (OSError, ValueError) as e:
        print(f"jobs hook FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
