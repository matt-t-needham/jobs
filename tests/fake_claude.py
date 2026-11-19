"""Stand-in for `claude -p` in stream-json mode, for the broker's tests.

Reads user messages from stdin, answers each with a text delta, the full text, and
a result, the way Claude Code's stream does. Appends its argv to $FAKE_ARGS_LOG so
tests can see how the broker started it. A message containing SLOW takes 1.5 s.
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if os.environ.get("FAKE_ARGS_LOG"):
    with open(os.environ["FAKE_ARGS_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")
sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": sid})
for line in sys.stdin:
    msg = json.loads(line)
    text = msg["message"]["content"]
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": "echo"}}})
    if "SLOW" in text:
        time.sleep(1.5)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "echo: " + text}]}})
    emit({"type": "result", "is_error": False, "session_id": sid})
