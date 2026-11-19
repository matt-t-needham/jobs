import json
import subprocess
import time
from pathlib import Path

import pytest

from broker import broker as B

TOKEN = "good"
TODO = "## Pending\n- [0] Ask about retries\n- [1] Fix sync\n"


@pytest.fixture
def broker(tmp_path):
    todos = tmp_path / "todos"
    todos.mkdir()
    (todos / "jobs.md").write_text(TODO)
    (todos / "GUIDE.md").write_text("not a list")
    state = tmp_path / "state"
    cfg = B.Settings(socket_path=state / "b.sock", approval_path=state / "a.sock", state_dir=state,
                     claude_bin="/bin/false", claude_home=tmp_path / "claude", roots=[tmp_path],
                     aud="aud", team="team", todos_dir=todos, repos={"jobs": "owner/jobs"})
    return B.Broker(cfg, verify=lambda t: t == TOKEN)


def call(b, **req):
    return b.dispatch(dict(req, token=TOKEN))


def item_id(b, text):
    items = call(b, action="todos")["projects"][0]["items"]
    return next(i["id"] for i in items if i["text"] == text)


def test_todos_lists_projects_and_skips_guide(broker):
    r = call(broker, action="todos")
    assert [p["name"] for p in r["projects"]] == ["jobs"]
    assert [(i["state"], i["text"]) for i in r["projects"][0]["items"]] == [
        (0, "Ask about retries"), (1, "Fix sync")]


def test_approving_writes_the_file_and_a_stale_approval_is_refused(broker):
    target = item_id(broker, "Ask about retries")
    r = call(broker, action="todo_set", project="jobs", todoId=target, expected=0, to=1)
    assert r["ok"] and r["state"] == 1
    assert "- [1] Ask about retries" in (broker.cfg.todos_dir / "jobs.md").read_text()
    again = call(broker, action="todo_set", project="jobs", todoId=target, expected=0, to=1)
    assert again["status"] == 409
    assert "- [1] Ask about retries" in (broker.cfg.todos_dir / "jobs.md").read_text()


def test_a_project_name_that_isnt_a_todo_file_is_not_found(broker):
    r = call(broker, action="todo_set", project="../etc", todoId="x", expected=0, to=1)
    assert r["status"] == 404


def test_pr_list_hides_old_merges_keeps_github_links_and_shows_errors(broker, monkeypatch):
    now = time.time()
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86400))
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 40 * 86400))
    rows = [
        {"number": 1, "title": "open", "url": "https://github.com/owner/jobs/pull/1", "state": "OPEN",
         "isDraft": True, "headRefName": "a", "updatedAt": fresh, "mergedAt": None},
        {"number": 2, "title": "merged long ago", "url": "https://github.com/owner/jobs/pull/2",
         "state": "MERGED", "isDraft": False, "headRefName": "b", "updatedAt": old, "mergedAt": old},
        {"number": 3, "title": "bad link", "url": "https://evil.example/3", "state": "OPEN",
         "isDraft": False, "headRefName": "c", "updatedAt": fresh, "mergedAt": None},
    ]

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["gh", "pr", "list"] and "owner/jobs" in cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")

    monkeypatch.setattr(B.subprocess, "run", fake_run)
    repo = call(broker, action="prs")["repos"][0]
    assert repo["error"] is None
    assert [p["number"] for p in repo["prs"]] == [1, 3]
    assert [p["url"] for p in repo["prs"]] == ["https://github.com/owner/jobs/pull/1", None]

    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "HTTP 401: bad credentials\nmore")

    broker.pr_cache.clear()
    monkeypatch.setattr(B.subprocess, "run", failing)
    repo = call(broker, action="prs")["repos"][0]
    assert repo["prs"] == [] and repo["error"] == "HTTP 401: bad credentials"
