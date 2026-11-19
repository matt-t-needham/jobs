import json
import os
import time
from pathlib import Path

import pytest

import collector
from collector import Collector, load_config

NOW = int(time.time() * 1000)
MIN = 60_000
DEAD_PID = 999_999_999  # beyond any pid_max


def own_start():
    return collector.proc_start(os.getpid())


@pytest.fixture
def env(tmp_path):
    claude = tmp_path / "claude"
    for d in ("sessions", "jobs", "projects"):
        (claude / d).mkdir(parents=True)
    spool = tmp_path / "spool"
    spool.mkdir()
    home = tmp_path / "home"
    (home / "apps" / "health").mkdir(parents=True)
    (home / "work" / "repo" / ".git").mkdir(parents=True)
    (home / "work" / "repo" / "src").mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "machine": {"id": "m1", "label": "Machine"},
        "claudeDir": str(claude),
        "spoolDir": str(spool),
        "projects": [
            {"match": str(home / "apps"), "id": "bix", "name": "bix", "repo": "first-dir"},
            {"match": str(home / "work"), "name": "Work"},
        ],
    }))
    return {"claude": claude, "spool": spool, "home": home, "cfg": load_config(cfg)}


def session(env, pid, sid, cwd, status="idle", kind="interactive", **extra):
    d = {"pid": pid, "sessionId": sid, "cwd": str(cwd), "kind": kind, "name": f"name-{sid}",
         "status": status, "statusUpdatedAt": NOW - MIN, "updatedAt": NOW - MIN,
         "startedAt": NOW - 10 * MIN, "version": "2.1.327",
         "procStart": own_start() if pid == os.getpid() else "1"}
    d.update(extra)
    (env["claude"] / "sessions" / f"{pid}.json").write_text(json.dumps(d))


def job(env, jid, sid, cwd, state, updated_ms=None, **extra):
    d = {"sessionId": sid, "cwd": str(cwd), "state": state, "name": f"job {jid}",
         "detail": "doing a thing", "needs": None, "tokens": 1234,
         "createdAt": iso(NOW - 30 * MIN), "updatedAt": iso(updated_ms or NOW - MIN),
         "cliVersion": "2.1.327",
         "providerEnv": {"ANTHROPIC_API_KEY": "sk-secret-value"}, "suggestedReply": "yes do it"}
    d.update(extra)
    (env["claude"] / "jobs" / jid).mkdir()
    (env["claude"] / "jobs" / jid / "state.json").write_text(json.dumps(d))


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + ".000Z"


def tasks(snap):
    return {t["id"]: t for t in snap["tasks"]}


def test_live_session_maps_to_project_and_repo(env):
    session(env, os.getpid(), "s1", env["home"] / "apps" / "health" / "relay", status="busy")
    t = tasks(Collector(env["cfg"]).snapshot())["s1"]
    assert t["projectId"] == "bix"
    assert t["repo"] == "health"
    assert t["status"] == "working"
    assert t["alive"] is True
    assert t["machineId"] == "m1"


def test_git_rule_labels_repo_by_git_root(env):
    session(env, os.getpid(), "s1", env["home"] / "work" / "repo" / "src")
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["repo"] == "repo"


def test_unmatched_cwd_is_ignored(env):
    session(env, os.getpid(), "s1", env["home"] / "elsewhere")
    assert Collector(env["cfg"]).snapshot()["tasks"] == []


def test_prefix_without_separator_does_not_match(env):
    session(env, os.getpid(), "s1", str(env["home"] / "apps") + "-old")
    assert Collector(env["cfg"]).snapshot()["tasks"] == []


def test_dead_pid_is_dead_then_hidden(env):
    session(env, DEAD_PID, "s1", env["home"] / "apps")
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "dead"
    session(env, DEAD_PID, "s1", env["home"] / "apps", statusUpdatedAt=NOW - 120 * MIN,
            updatedAt=NOW - 120 * MIN)
    assert Collector(env["cfg"]).snapshot()["tasks"] == []


def test_reused_pid_with_other_start_time_is_dead(env):
    if collector.proc_start(os.getpid()) is None:
        pytest.skip("needs /proc")
    session(env, os.getpid(), "s1", env["home"] / "apps", procStart="12345")
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "dead"


def test_other_pid_domain_is_skipped(env):
    session(env, DEAD_PID, "s1", env["home"] / "apps", pidDomain="linux:other:pid:[1]")
    c = Collector(env["cfg"])
    c.pid_domain = "linux:mine:pid:[2]"
    assert c.snapshot()["tasks"] == []


def test_blocked_job_needs_you_with_needs_line(env):
    job(env, "j1", "s1", env["home"] / "apps", "blocked", needs="Should I merge?")
    t = tasks(Collector(env["cfg"]).snapshot())["s1"]
    assert t["status"] == "needs-you"
    assert t["needs"] == "Should I merge?"
    assert t["kind"] == "bg"
    assert t["name"] == "job j1"
    assert t["tokens"] == 1234


def test_job_overrides_session_status(env):
    session(env, os.getpid(), "s1", env["home"] / "apps", status="busy", kind="bg")
    job(env, "j1", "s1", env["home"] / "apps", "blocked", needs="?")
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "needs-you"


def test_old_jobs_are_hidden(env):
    job(env, "j1", "s1", env["home"] / "apps", "working", updated_ms=NOW - 200 * 3_600_000)
    assert Collector(env["cfg"]).snapshot()["tasks"] == []


def test_working_without_activity_is_stalled(env):
    job(env, "j1", "s1", env["home"] / "apps", "working", updated_ms=NOW - 30 * MIN)
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "stalled"


def test_done_hidden_after_window(env):
    job(env, "j1", "s1", env["home"] / "apps", "done", updated_ms=NOW - 5 * MIN)
    job(env, "j2", "s2", env["home"] / "apps", "done", updated_ms=NOW - 90 * MIN)
    ids = set(tasks(Collector(env["cfg"]).snapshot()))
    assert ids == {"s1"}


def test_snapshot_never_carries_unlisted_fields(env):
    job(env, "j1", "s1", env["home"] / "apps", "blocked", needs="x")
    session(env, os.getpid(), "s2", env["home"] / "apps", messagingSocketPath="/tmp/sock")
    snap = Collector(env["cfg"]).snapshot()
    raw = json.dumps(snap)
    assert "sk-secret-value" not in raw
    assert "yes do it" not in raw
    assert "/tmp/sock" not in raw
    for t in snap["tasks"]:
        assert tuple(t) == collector.TASK_KEYS


def test_format_change_is_a_warning_not_a_tile(env):
    (env["claude"] / "sessions" / "1.json").write_text(json.dumps({"pid": 1, "version": "9.9.9"}))
    snap = Collector(env["cfg"]).snapshot()
    assert snap["tasks"] == []
    assert any("format changed" in w and "9.9.9" in w for w in snap["warnings"])


def test_unknown_status_value_warns(env):
    session(env, os.getpid(), "s1", env["home"] / "apps", status="pondering")
    snap = Collector(env["cfg"]).snapshot()
    assert any("unknown session status 'pondering'" in w for w in snap["warnings"])
    assert tasks(snap)["s1"]["status"] == "idle"


def test_non_directory_in_jobs_is_ignored(env):
    (env["claude"] / "jobs" / "pins.json").write_text("{}")
    assert Collector(env["cfg"]).snapshot()["warnings"] == []


def test_torn_session_file_reported_only_when_it_stays_broken(env):
    (env["claude"] / "sessions" / "5.json").write_text('{"pid": 5, "sess')
    c = Collector(env["cfg"])
    assert not any("cannot parse" in w for w in c.snapshot()["warnings"])
    assert any("cannot parse 5.json" in w for w in c.snapshot()["warnings"])


def test_hook_notification_newer_than_status_means_needs_you(env):
    session(env, os.getpid(), "s1", env["home"] / "apps", status="busy")
    (env["spool"] / "1.json").write_text(json.dumps({
        "event": "Notification", "sessionId": "s1", "at": NOW,
        "notificationType": "permission_prompt", "message": "Claude needs your permission to use Bash"}))
    c = Collector(env["cfg"])
    t = tasks(c.snapshot())["s1"]
    assert t["status"] == "needs-you"
    assert t["needs"] == "Claude needs your permission to use Bash"
    assert list(env["spool"].iterdir()) == []  # consumed
    # A later session status change wins over the older hook event.
    session(env, os.getpid(), "s1", env["home"] / "apps", status="busy", statusUpdatedAt=NOW + 1000)
    assert tasks(c.snapshot())["s1"]["status"] == "working"


def test_idle_prompt_notification_is_not_needs_you(env):
    session(env, os.getpid(), "s1", env["home"] / "apps", status="busy")
    (env["spool"] / "1.json").write_text(json.dumps({
        "event": "Notification", "sessionId": "s1", "at": NOW, "notificationType": "idle_prompt"}))
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "idle"


def test_bad_hook_file_is_reported_and_removed(env):
    (env["spool"] / "1.json").write_text("not json")
    snap = Collector(env["cfg"]).snapshot()
    assert any("bad hook event 1.json" in w for w in snap["warnings"])
    assert list(env["spool"].iterdir()) == []


def test_transcript_tail_gives_branch_and_cost_incrementally(env):
    cwd = env["home"] / "apps" / "health"
    session(env, os.getpid(), "s1", cwd)
    tdir = env["claude"] / "projects" / str(cwd).replace("/", "-").replace("_", "-")
    tdir.mkdir()
    tpath = tdir / "s1.jsonl"
    tpath.write_text(json.dumps({"type": "user", "gitBranch": "main"}) + "\n")
    c = Collector(env["cfg"])
    t = tasks(c.snapshot())["s1"]
    assert t["branch"] == "main"
    assert t["costUsd"] is None
    with tpath.open("a") as f:
        f.write(json.dumps({"type": "cost-state", "totalCostUSD": 1.23456}) + "\n")
        f.write(json.dumps({"type": "assistant", "gitBranch": "feat-x"}))  # no newline yet
    t = tasks(c.snapshot())["s1"]
    assert t["costUsd"] == 1.2346
    assert t["branch"] == "main"  # the unfinished line waits
    with tpath.open("a") as f:
        f.write("\n")
    assert tasks(c.snapshot())["s1"]["branch"] == "feat-x"


def test_transcript_first_read_skips_partial_line(env, monkeypatch):
    monkeypatch.setattr(collector, "TRANSCRIPT_FIRST_READ", 50)
    p = env["claude"] / "projects" / "x.jsonl"
    p.write_text(json.dumps({"gitBranch": "old-" + "a" * 100}) + "\n" + json.dumps({"gitBranch": "new"}) + "\n")
    info = collector.Transcripts(env["claude"] / "projects").read(p)
    assert info.branch == "new"


def test_long_text_is_truncated_and_whitespace_collapsed(env):
    job(env, "j1", "s1", env["home"] / "apps", "blocked", needs="a\n  b " + "x" * 400)
    needs = tasks(Collector(env["cfg"]).snapshot())["s1"]["needs"]
    assert needs.startswith("a b x")
    assert len(needs) == collector.TEXT_LIMITS["needs"]
    assert needs.endswith("…")


def test_tasks_sorted_by_status_then_recency(env):
    job(env, "j1", "s1", env["home"] / "apps", "done", updated_ms=NOW - 2 * MIN)
    job(env, "j2", "s2", env["home"] / "apps", "blocked", needs="?")
    job(env, "j3", "s3", env["home"] / "apps", "working", updated_ms=NOW - MIN)
    assert [t["id"] for t in Collector(env["cfg"]).snapshot()["tasks"]] == ["s2", "s3", "s1"]


def test_write_snapshot_is_atomic_and_private(tmp_path):
    out = tmp_path / "state.json"
    collector.write_snapshot(out, {"a": 1})
    assert json.loads(out.read_text()) == {"a": 1}
    assert out.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.parametrize("bad, msg", [
    ({"projects": []}, "projects is empty"),
    ({"projects": [{"match": "~/x"}]}, "needs string match and name"),
    ({"projects": [{"match": "~/x", "name": "x", "repo": "svn"}]}, "repo must be"),
    ({"projects": [{"match": "~/x", "name": "x"}], "pollSeconds": 0}, "pollSeconds"),
])
def test_bad_config_is_refused(tmp_path, bad, msg):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(collector.ConfigError, match=msg):
        load_config(p)


def test_iso_ms_accepts_z_suffix():
    assert collector.iso_ms("1970-01-01T00:00:01.500Z") == 1500
    assert collector.iso_ms("nonsense") is None


def test_once_does_not_consume_hook_events(env, tmp_path, capsys):
    cfgp = tmp_path / "c2.json"
    cfgp.write_text(json.dumps({"claudeDir": str(env["claude"]), "spoolDir": str(env["spool"]),
                                "projects": [{"match": str(env["home"]), "name": "h"}]}))
    (env["spool"] / "1.json").write_text(json.dumps({"event": "Stop", "sessionId": "s", "at": NOW}))
    assert collector.main(["--config", str(cfgp), "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert (env["spool"] / "1.json").exists()


def test_idle_session_with_fresh_transcript_is_working(env):
    cwd = env["home"] / "apps"
    session(env, os.getpid(), "s1", cwd, status="idle", statusUpdatedAt=NOW - 5 * MIN)
    tdir = env["claude"] / "projects" / str(cwd).replace("/", "-")
    tdir.mkdir()
    (tdir / "s1.jsonl").write_text("{}\n")  # mtime: now
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "working"


def test_idle_session_with_old_transcript_stays_idle(env):
    cwd = env["home"] / "apps"
    session(env, os.getpid(), "s1", cwd, status="idle", statusUpdatedAt=NOW)
    tdir = env["claude"] / "projects" / str(cwd).replace("/", "-")
    tdir.mkdir()
    p = tdir / "s1.jsonl"
    p.write_text("{}\n")
    os.utime(p, (time.time() - 600, time.time() - 600))
    assert tasks(Collector(env["cfg"]).snapshot())["s1"]["status"] == "idle"
