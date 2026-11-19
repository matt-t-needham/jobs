import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import collector
from broker import broker as B

HERE = Path(__file__).resolve().parent
TOKEN = "good"


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def fake_bin(tmp_path):
    path = tmp_path / "fake-claude"
    path.write_text(f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fake_claude.py'} \"$@\"\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def broker(tmp_path, fake_bin, monkeypatch):
    apps = tmp_path / "apps"
    (apps / "proj").mkdir(parents=True)
    state = tmp_path / "state"
    cfg = B.Settings(socket_path=state / "broker.sock", approval_path=state / "approval.sock", state_dir=state, claude_bin=str(fake_bin),
                     claude_home=tmp_path / "claude", roots=[apps], aud="aud", team="team",
                     approval_timeout=2.0, release_wait=5.0, terminal_stop_wait=5.0)
    args_log = tmp_path / "args.log"
    monkeypatch.setenv("FAKE_ARGS_LOG", str(args_log))
    (tmp_path / "claude" / "sessions").mkdir(parents=True)
    b = B.Broker(cfg, verify=lambda tok: tok == TOKEN)
    b.args_log = args_log
    b.project = str(apps / "proj")
    yield b
    for s in list(b.drivers.values()):
        s.stop(5)


def call(b, **req):
    return b.dispatch(dict(req, token=TOKEN))


def start(b, text="hello", **extra):
    r = call(b, action="start", cwd=b.project, text=text, clientId=uuid.uuid4().hex, **extra)
    assert r["ok"], r
    return r["sessionId"]


def turn_done(b, sid, after=0):
    r = call(b, action="events", sessionId=sid, after=after)
    return r["events"] and any(e["kind"] == "turn_end" for e in r["events"])


def test_refused_without_a_valid_access_token(broker):
    r = broker.dispatch({"action": "events", "sessionId": str(uuid.uuid4()), "after": 0, "token": "nope"})
    assert r["status"] == 403
    assert "refused" in (broker.cfg.state_dir / "audit.log").read_text()


def test_start_refuses_a_directory_outside_the_projects(broker, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    r = call(broker, action="start", cwd=str(tmp_path / "elsewhere"), text="hi", clientId="c1")
    assert r["status"] == 403


def test_events_stream_in_order_and_after_pages_only_newer_ones(broker):
    sid = start(broker, "first")
    assert wait_for(lambda: turn_done(broker, sid))
    events = call(broker, action="events", sessionId=sid, after=0)["events"]
    kinds = [e["kind"] for e in events]
    assert kinds[:4] == ["user", "delta", "text", "turn_end"]
    assert events[2]["text"] == "echo: first"
    last = events[-1]["seq"]
    assert call(broker, action="events", sessionId=sid, after=last)["events"] == []


def test_a_repeated_client_id_is_delivered_once(broker):
    sid = start(broker, "once")
    assert wait_for(lambda: turn_done(broker, sid))
    r = call(broker, action="send", sessionId=sid, text="again", clientId="same", cwd=broker.project)
    assert r["ok"] and r["duplicate"] is False
    assert wait_for(lambda: not call(broker, action="events", sessionId=sid, after=0)["busy"])
    r = call(broker, action="send", sessionId=sid, text="again", clientId="same", cwd=broker.project)
    assert r["ok"] and r["duplicate"] is True
    users = [e for e in call(broker, action="events", sessionId=sid, after=0)["events"] if e["kind"] == "user"]
    assert [u["text"] for u in users] == ["once", "again"]


def test_a_send_while_the_session_is_answering_is_refused(broker):
    sid = start(broker, "SLOW one")
    r = call(broker, action="send", sessionId=sid, text="two", clientId="c2", cwd=broker.project)
    assert r["status"] == 409


def test_approval_waits_for_the_web_and_takes_its_decision(broker):
    sid = start(broker, "needs a tool")
    ticket = next(iter(broker.drivers.values())).ticket
    result = {}
    t = threading.Thread(target=lambda: result.update(
        broker.dispatch({"action": "approval_request", "ticket": ticket,
                         "tool_name": "Bash", "input": {"command": "rm -rf /tmp/x"}}, "approval")))
    t.start()
    assert wait_for(lambda: broker.drivers[sid].pending())
    ap = broker.drivers[sid].pending()[0]
    assert ap["summary"] == "Bash: rm -rf /tmp/x"
    assert call(broker, action="approve", sessionId=sid, approvalId=ap["id"], allow=True)["ok"]
    t.join(5)
    assert result == {"ok": True, "allow": True}


def test_an_approval_nobody_answers_is_denied(broker):
    sid = start(broker, "waits")
    ticket = next(iter(broker.drivers.values())).ticket
    resp = broker.dispatch({"action": "approval_request", "ticket": ticket, "tool_name": "Bash", "input": {}},
                           "approval")
    assert resp == {"ok": True, "allow": False}
    events = call(broker, action="events", sessionId=sid, after=0)["events"]
    assert any(e["kind"] == "approval_decided" and e["reason"] == "timed out" for e in events)


def hold_terminal(broker, status, quiet_for):
    """A live process holding a session, and a transcript last written quiet_for seconds ago."""
    child = subprocess.Popen(["sleep", "60"])
    sid = str(uuid.uuid4())
    (broker.cfg.claude_home / "sessions" / f"{child.pid}.json").write_text(json.dumps({
        "pid": child.pid, "sessionId": sid, "cwd": broker.project, "kind": "interactive",
        "status": status, "procStart": collector.proc_start(child.pid)}))
    tdir = broker.cfg.claude_home / "projects" / "-x"
    tdir.mkdir(parents=True, exist_ok=True)
    t = tdir / f"{sid}.jsonl"
    t.write_text(json.dumps({"type": "user", "cwd": broker.project}) + "\n")
    os.utime(t, (time.time() - quiet_for, time.time() - quiet_for))
    return child, sid


def test_a_terminal_holds_the_session_until_taken_over(broker):
    child, sid = hold_terminal(broker, "idle", quiet_for=60)
    try:
        assert call(broker, action="events", sessionId=sid, after=0)["driver"] == "terminal"
        r = call(broker, action="send", sessionId=sid, text="hi", clientId="c")
        assert r["status"] == 409
        r = call(broker, action="take_over", sessionId=sid)
        assert r["ok"] and r["released"] is True
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_take_over_waits_for_a_terminal_that_is_still_answering(broker):
    # The session file can say idle mid-turn, so a recently written transcript counts as busy.
    child, sid = hold_terminal(broker, "idle", quiet_for=2)
    try:
        r = call(broker, action="take_over", sessionId=sid)
        assert r["status"] == 409
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_after_a_stop_the_next_send_resumes_the_same_session(broker):
    sid = start(broker, "remember 4242")
    assert wait_for(lambda: turn_done(broker, sid))
    before = call(broker, action="events", sessionId=sid, after=0)["events"][-1]["seq"]
    assert call(broker, action="stop", sessionId=sid)["ok"]
    assert wait_for(lambda: sid not in broker.drivers)
    # The conversation on disk: every transcript record carries the session's cwd.
    tdir = broker.cfg.claude_home / "projects" / "-x"
    tdir.mkdir(parents=True)
    (tdir / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "cwd": broker.project}) + "\n")
    r = call(broker, action="send", sessionId=sid, text="what number?", clientId="c3")
    assert r["ok"] and r["sessionId"] == sid
    # A client that polled before the stop keeps its cursor: the resumed turn is numbered on.
    assert wait_for(lambda: turn_done(broker, sid, after=before))
    assert wait_for(lambda: len(broker.args_log.read_text().splitlines()) == 2)
    started = [json.loads(line) for line in broker.args_log.read_text().splitlines()]
    assert started[0][started[0].index("--session-id") + 1] == sid
    assert started[-1][started[-1].index("--resume") + 1] == sid


def test_audit_records_actions_but_never_the_text(broker):
    start(broker, "SECRET-PHRASE-4711")
    log = (broker.cfg.state_dir / "audit.log").read_text()
    assert "start" in log and "ok=yes" in log
    assert "SECRET-PHRASE" not in log


def test_the_web_socket_never_takes_an_approval_request(broker):
    """Holding a driver's ticket must not let a web-side caller approve its own tool calls."""
    sid = start(broker, "x")
    ticket = next(iter(broker.drivers.values())).ticket
    r = broker.dispatch({"action": "approval_request", "ticket": ticket, "tool_name": "Bash", "input": {}},
                        "web")
    assert r["status"] == 404
    assert call(broker, action="events", sessionId=sid, after=0)["approvals"] == []


def test_the_approval_socket_takes_nothing_else(broker):
    r = broker.dispatch({"action": "start", "cwd": broker.project, "text": "hi", "token": TOKEN}, "approval")
    assert r["status"] == 404


def test_history_shows_the_transcript_of_a_session_nobody_is_driving(broker):
    """A terminal session has no live events, so the Open view must read its transcript."""
    sid = str(uuid.uuid4())
    tdir = broker.cfg.claude_home / "projects" / "-x"
    tdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "user", "message": {"content": "deploy with token=abc123def456"}, "timestamp": "2026-11-19T01:00:00.000Z"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Running it now."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo SECRET-INPUT-999"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}},
        {"type": "user", "message": {"content": "<system-reminder>skip me</system-reminder>"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Failed; see above."}]}},
    ]
    (tdir / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    r = call(broker, action="history", sessionId=sid)
    assert r["ok"] and r["cursor"] == 0
    kinds = [(i["kind"], i.get("text") or i.get("name") or "") for i in r["items"]]
    assert kinds == [("user", "deploy with token=[redacted]"), ("text", "Running it now."),
                     ("tool", "Bash"), ("tool_result", ""), ("text", "Failed; see above.")]
    assert "SECRET-INPUT-999" not in json.dumps(r)


def test_the_model_is_always_the_top_one_whatever_the_request_asks(broker):
    r = call(broker, action="start", cwd=broker.project, text="hi", model="haiku", clientId="m1")
    assert r["ok"]
    assert wait_for(lambda: broker.args_log.exists() and broker.args_log.read_text().strip())
    started = json.loads(broker.args_log.read_text().splitlines()[0])
    assert started[started.index("--model") + 1] == "opus"


def test_a_cursor_ahead_of_this_broker_asks_the_page_to_reload(broker):
    """After a broker restart the numbering starts again; a stale cursor must say so."""
    sid = start(broker, "one")
    assert wait_for(lambda: turn_done(broker, sid))
    assert call(broker, action="events", sessionId=sid, after=0)["reset"] is False
    assert call(broker, action="events", sessionId=sid, after=10_000)["reset"] is True
