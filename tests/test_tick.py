import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from broker import tick as TICK
from broker import todos as TODOS

PROJECTS = ["alpha", "bravo", "charlie", "delta"]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Four projects with one approved todo each, real git remotes and clones, and a fake agent."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")

    def repo_with_remote(name):
        origin = tmp_path / f"{name}-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = tmp_path / f"{name}-seed"
        subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
        (seed / "README.md").write_text(name)
        git(seed, "add", "README.md")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "main")
        clone = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        return clone

    folders = {p: repo_with_remote(p) for p in PROJECTS}
    todos_origin = tmp_path / "todos-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(todos_origin)], check=True)
    todos_repo = tmp_path / "todos-repo"
    subprocess.run(["git", "clone", "-q", str(todos_origin), str(todos_repo)], check=True)
    (todos_repo / "todos").mkdir()
    for p in PROJECTS:
        (todos_repo / "todos" / f"{p}.md").write_text(f"## Pending\n- [1] Todo for {p}\n")
    git(todos_repo, "add", "todos")
    git(todos_repo, "commit", "-q", "-m", "todos")
    git(todos_repo, "push", "-q", "origin", "main")

    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/sh\nsleep 30\n")  # a live agent for the length of the test
    fake.chmod(0o755)
    cfg = tmp_path / "broker.json"
    cfg.write_text(json.dumps({
        "todosDir": str(todos_repo / "todos"), "todosRepo": str(todos_repo),
        "stateDir": str(tmp_path / "state"), "claudeBin": str(fake),
        "repos": {}, "projectDirs": {p: str(folders[p]) for p in PROJECTS},
    }))
    yield {"cfg": cfg, "todos_repo": todos_repo, "state": tmp_path / "state"}
    agents = tmp_path / "state" / "agents.json"
    if agents.exists():
        for entry in json.loads(agents.read_text()).values():
            if entry["pid"] == os.getpid():
                continue  # the cap test borrows our own pid as a "live" agent; never signal it
            try:
                os.kill(entry["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_tick_starts_three_projects_and_records_the_moves(world):
    rc = TICK.main(["--config", str(world["cfg"])], usage=lambda: (0, 0))
    assert rc == 0
    todos_dir = world["todos_repo"] / "todos"
    states = {p: TODOS.read_project(todos_dir / f"{p}.md")[0].state for p in PROJECTS}
    assert sorted(states.values()).count(2) == 3 and sorted(states.values()).count(1) == 1
    started = [p for p, s in states.items() if s == 2]
    log = git(world["todos_repo"], "log", "--format=%s").splitlines()
    assert sorted(log[:3]) == sorted(f"todos: {p} -> in progress" for p in started)
    agents = json.loads((world["state"] / "agents.json").read_text())
    assert len(agents) == 3
    for entry in agents.values():
        assert Path(entry["worktree"]).is_dir()
        assert entry["branch"].startswith("todo-")


def test_a_tick_at_the_cap_starts_nothing(world):
    state = world["state"]
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    live = {f"todo-x{n}": {"pid": os.getpid(), "project": "x", "todo": "x", "branch": f"todo-x{n}",
                           "started": 0, "worktree": ""} for n in range(TICK.MAX_RUNNING)}
    (state / "agents.json").write_text(json.dumps(live))
    before = git(world["todos_repo"], "rev-parse", "HEAD")
    assert TICK.main(["--config", str(world["cfg"])], usage=lambda: (0, 0)) == 0
    assert git(world["todos_repo"], "rev-parse", "HEAD") == before
    assert len(json.loads((state / "agents.json").read_text())) == TICK.MAX_RUNNING


SAMPLE = """You are currently using your subscription to power your Claude Code usage

Current session: 71% used · resets Nov 19, 11:19am (America/Los_Angeles)
Current week (all models): 37% used · resets Nov 22, 10:59am (America/Los_Angeles)
"""


def test_the_usage_figures_are_read_from_the_cli_output():
    assert TICK.parse_usage(SAMPLE) == (71, 37)
    with pytest.raises(RuntimeError, match="no session and week"):
        TICK.parse_usage("Current session: 71% used")


@pytest.mark.parametrize("session,week,blocked", [
    (85, 75, False),   # at the limits: still allowed
    (86, 10, True),    # one point over the session limit
    (10, 76, True),    # one point over the week limit
])
def test_the_usage_limits_are_strictly_above(session, week, blocked):
    assert (TICK.usage_block(session, week) is not None) == blocked


def test_a_tick_over_the_usage_limit_starts_nothing(world, capsys):
    before = git(world["todos_repo"], "rev-parse", "HEAD")
    assert TICK.main(["--config", str(world["cfg"])], usage=lambda: (10, 90)) == 0
    assert "NOT STARTING: the week is at 90%" in capsys.readouterr().out
    assert git(world["todos_repo"], "rev-parse", "HEAD") == before
    assert not (world["state"] / "agents.json").exists()


def test_a_tick_that_cannot_read_the_usage_starts_nothing(world, capsys):
    def broken():
        raise RuntimeError("claude /usage failed")
    before = git(world["todos_repo"], "rev-parse", "HEAD")
    assert TICK.main(["--config", str(world["cfg"])], usage=broken) == 1
    assert "FAILED: cannot read the usage" in capsys.readouterr().out
    assert git(world["todos_repo"], "rev-parse", "HEAD") == before
