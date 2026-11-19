#!/usr/bin/env python3
"""The daily project tick (workbench plan §12, DESIGN.md §12).

Cron runs this once a day, as matt. It does two things, in order:

  1. Closes out in-progress todos whose PR has merged (2 -> 3).
  2. Starts a project agent on the oldest approved todo of up to PROJECTS_PER_TICK
     projects (1 -> 2). A project with an item already in progress is skipped, and no more
     than MAX_RUNNING agents run at once.

An agent is `claude -p` in its own git worktree, on branch todo-<id>. It implements the
todo, opens a draft PR and stops. It never merges. The state change is committed to the
todos checkout (CLAUDE.md commit policy) once the agent has started.

    python3 -I broker/tick.py --config config/broker.json

Stdlib, plus the broker's todo module. Output is one line per action, for the cron log.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from broker import todos as TODOS  # noqa: E402

PROJECTS_PER_TICK = 3
MAX_RUNNING = 3          # agents this tick has started that are still running (not manual ones)
SESSION_LIMIT_PCT = 85   # "daily": the CLI's "Current session" figure
WEEK_LIMIT_PCT = 75      # "Current week (all models)"
USAGE_LINE = re.compile(r"^(Current session|Current week \(all models\)):\s*(\d+)% used", re.MULTILINE)
# What an agent may do without a person watching. Everything else is refused in -p mode.
ALLOWED_TOOLS = ",".join([
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git:*)", "Bash(gh pr create:*)", "Bash(gh pr view:*)",
    "Bash(python3:*)", "Bash(python3 -m pytest:*)", "Bash(docker build:*)",
])


def pick(projects: Dict[str, List[TODOS.Item]], busy: set) -> List[Tuple[str, TODOS.Item]]:
    """The oldest approved todo of each project that has none in progress, projects in name
    order, at most PROJECTS_PER_TICK of them."""
    chosen: List[Tuple[str, TODOS.Item]] = []
    for name in sorted(projects):
        if name in busy:
            continue
        approved = [i for i in projects[name] if i.state == 1]
        if approved:
            chosen.append((name, approved[0]))
        if len(chosen) == PROJECTS_PER_TICK:
            break
    return chosen


def parse_usage(text: str) -> Tuple[int, int]:
    """(session %, week %) from `claude -p /usage`. Raises if either figure is missing."""
    found = {label: int(pct) for label, pct in USAGE_LINE.findall(text)}
    session = found.get("Current session")
    week = found.get("Current week (all models)")
    if session is None or week is None:
        raise RuntimeError("the usage output has no session and week figures")
    return session, week


def read_usage(claude_bin: str) -> Tuple[int, int]:
    """The subscription's usage, from the CLI. Any failure is an error, so nothing starts."""
    try:
        out = subprocess.run([claude_bin, "-p", "/usage", "--output-format", "text"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"claude /usage could not run: {e.__class__.__name__}")
    if out.returncode != 0:
        raise RuntimeError("claude /usage failed")
    return parse_usage(out.stdout)


def usage_block(session_pct: int, week_pct: int) -> Optional[str]:
    """Why no agent may start, or None when the usage is under both limits."""
    if week_pct > WEEK_LIMIT_PCT:
        return f"the week is at {week_pct}% (limit {WEEK_LIMIT_PCT}%)"
    if session_pct > SESSION_LIMIT_PCT:
        return f"the current session is at {session_pct}% (limit {SESSION_LIMIT_PCT}%)"
    return None


def pid_running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state not in ("Z", "X")


def live_agents(agents: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in agents.items() if pid_running(int(v["pid"]))}


def run(cmd: List[str], cwd: Path) -> str:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        first = (out.stderr.strip().splitlines() or [f"{cmd[0]} failed"])[0]
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {first[:200]}")
    return out.stdout


def default_branch(folder: Path) -> str:
    ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], folder).strip()
    return ref.split("/", 1)[1] if "/" in ref else ref


def prompt_for(project: str, item: TODOS.Item, branch: str) -> str:
    return (
        f"You are the project agent for {project}. Work this approved todo, and only this one:\n\n"
        f"    {item.text}\n\n"
        "1. Read this project's CLAUDE.md and follow it.\n"
        f"2. Work in this worktree, on the branch {branch}. Keep the change to the todo.\n"
        "3. Run the project's tests and make sure they pass.\n"
        "4. Commit with a short message, push the branch, and open a DRAFT pull request with "
        "`gh pr create --draft`. Never merge it, and never mark it ready.\n"
        "5. Stop when the draft is open, and print its URL.\n"
    )


def start_agent(project: str, item: TODOS.Item, folder: Path, state_dir: Path,
                claude_bin: str) -> Dict[str, Any]:
    branch = f"todo-{item.id}"
    worktree = folder / ".claude" / "worktrees" / branch
    run(["git", "fetch", "-q", "origin"], folder)
    base = f"origin/{default_branch(folder)}"
    run(["git", "worktree", "add", str(worktree), "-b", branch, base], folder)
    log_dir = state_dir / "agents"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = open(log_dir / f"{branch}.log", "ab")
    proc = subprocess.Popen(
        [claude_bin, "-p", prompt_for(project, item, branch), "--model", "opus",
         "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS],
        cwd=worktree, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True)
    return {"project": project, "todo": item.text, "branch": branch, "pid": proc.pid,
            "started": int(time.time()), "worktree": str(worktree)}


def commit_state(todos_repo: Path, path: Path, project: str, to: int) -> None:
    TODOS.commit_file(todos_repo, path, f"todos: {project} -> {TODOS.STATES[to]}")


def close_merged(projects: Dict[str, Tuple[Path, List[TODOS.Item]]], repos: Dict[str, str],
                 todos_repo: Path) -> List[str]:
    """2 -> 3 for each in-progress todo whose todo-<id> PR has merged."""
    done = []
    for name, (path, items) in sorted(projects.items()):
        repo = repos.get(name)
        if not repo:
            continue
        for it in [i for i in items if i.state == 2]:
            branch = f"todo-{it.id}"
            try:
                merged = json.loads(run(["gh", "pr", "list", "--repo", repo, "--head", branch,
                                         "--state", "merged", "--json", "number"], todos_repo) or "[]")
            except (RuntimeError, ValueError) as e:
                print(f"FAILED {name}: cannot check PR for {branch}: {e}")
                continue
            if merged:
                path.write_text(TODOS.set_state(path.read_text(), it.id, 2, 3))
                commit_state(todos_repo, path, name, 3)
                done.append(f"{name}: {it.text} (PR merged)")
    return done


def main(argv: Optional[List[str]] = None, usage: Optional[Callable[[], Tuple[int, int]]] = None) -> int:
    ap = argparse.ArgumentParser(description="daily project tick")
    ap.add_argument("--config", required=True, type=Path)
    args = ap.parse_args(argv)
    raw = json.loads(args.config.read_text())
    todos_dir = Path(os.path.expanduser(raw["todosDir"]))
    todos_repo = Path(os.path.expanduser(raw["todosRepo"]))
    state_dir = Path(os.path.expanduser(raw.get("stateDir", "~/jobs/broker")))
    claude_bin = os.path.expanduser(raw.get("claudeBin", "~/.local/bin/claude"))
    usage_of = usage or (lambda: read_usage(claude_bin))
    repos: Dict[str, str] = raw.get("repos", {})
    dirs: Dict[str, str] = raw.get("projectDirs", {})

    projects: Dict[str, Tuple[Path, List[TODOS.Item]]] = {}
    for path in TODOS.project_files(todos_dir):
        projects[path.stem] = (path, TODOS.read_project(path))

    for line in close_merged(projects, repos, todos_repo):
        print(f"CLOSED {line}")

    # The usage gate: a tick never starts an agent when the subscription is close to its limits,
    # and it never starts one when the usage can't be read.
    try:
        session_pct, week_pct = usage_of()
    except RuntimeError as e:
        print(f"FAILED: cannot read the usage, so nothing is started: {e}")
        return 1
    why = usage_block(session_pct, week_pct)
    if why:
        print(f"NOT STARTING: {why}")
        return 0

    agents_file = state_dir / "agents.json"
    agents = json.loads(agents_file.read_text()) if agents_file.exists() else {}
    agents = live_agents(agents)
    room = MAX_RUNNING - len(agents)
    if room <= 0:
        print(f"AT THE CAP: {len(agents)} agents running; nothing started")
        agents_file.write_text(json.dumps(agents))
        return 0

    busy = {name for name, (_, items) in projects.items() if any(i.state == 2 for i in items)}
    plain = {name: items for name, (_, items) in projects.items()}
    failures = 0
    for name, item in pick(plain, busy)[:room]:
        path = projects[name][0]
        folder = Path(os.path.expanduser(dirs.get(name, "")))
        if not dirs.get(name) or not folder.is_dir():
            print(f"FAILED {name}: no local folder configured (projectDirs)")
            failures += 1
            continue
        try:
            entry = start_agent(name, item, folder, state_dir, claude_bin)
        except (RuntimeError, OSError) as e:
            print(f"FAILED {name}: could not start an agent: {e}")
            failures += 1
            continue
        agents[entry["branch"]] = entry
        try:
            path.write_text(TODOS.set_state(path.read_text(), item.id, 1, 2))
            commit_state(todos_repo, path, name, 2)
        except (TODOS.TodoError, TODOS.CommitError, OSError) as e:
            # The agent is running; the todo's state didn't get saved. Say so.
            print(f"FAILED {name}: agent started (pid {entry['pid']}) but the todo state wasn't saved: {e}")
            failures += 1
            continue
        print(f"STARTED {name}: {item.text} (branch {entry['branch']}, pid {entry['pid']})")

    agents_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    agents_file.write_text(json.dumps(agents))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
