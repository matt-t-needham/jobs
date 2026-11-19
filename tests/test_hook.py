import json
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent.parent / "hook.py")


def run(stdin, spool):
    return subprocess.run([sys.executable, "-I", HOOK, "--spool", str(spool)],
                          input=stdin, capture_output=True, text=True, timeout=10)


def test_notification_keeps_only_allowed_fields(tmp_path):
    r = run(json.dumps({
        "hook_event_name": "Notification", "session_id": "s1",
        "notification_type": "permission_prompt", "message": "m" * 500,
        "transcript_path": "/secret/path.jsonl", "cwd": "/home/x",
    }), tmp_path)
    assert r.returncode == 0, r.stderr
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"
    rec = json.loads(files[0].read_text())
    assert set(rec) == {"event", "sessionId", "at", "notificationType", "message"}
    assert len(rec["message"]) == 300


def test_prompt_text_is_never_written(tmp_path):
    r = run(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                        "prompt": "my secret prompt"}), tmp_path)
    assert r.returncode == 0, r.stderr
    rec = json.loads(next(tmp_path.iterdir()).read_text())
    assert set(rec) == {"event", "sessionId", "at"}


def test_failure_exits_1_with_message(tmp_path):
    r = run("not json", tmp_path)
    assert r.returncode == 1
    assert "jobs hook FAILED" in r.stderr
    r = run(json.dumps({"hook_event_name": "Stop", "session_id": "s"}), tmp_path / "missing")
    assert r.returncode == 1
    assert "FAILED" in r.stderr
