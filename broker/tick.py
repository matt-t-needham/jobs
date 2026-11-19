#!/usr/bin/env python3
"""The project tick (workbench plan §12, DESIGN.md §12).

Cron runs this every ten minutes from 22:00 to 04:50, as matt. It acts once per
five-hour usage window: at the first run after the window begins, when that window began
after 22:00 local time. Then, in order:

  1. Checks the subscription usage (`claude -p /usage`). It starts nothing if the week is at
     or above WEEK_LIMIT_PCT, or the current 5-hour session is at or above SESSION_LIMIT_PCT
     (it must be below 80%), or the usage can't be read.
  2. Closes out in-progress todos whose PR has merged (2 -> 3).
  3. Starts a project agent on the oldest approved todo of up to PROJECTS_PER_TICK
     projects (1 -> 2). A project with an item already in progress is skipped. At most
     MAX_RUNNING agents that this tick started are running at once; agents started by hand
     don't count.

An agent is `claude -p` (Opus) in its own git worktree, on branch todo-<id>. It implements
the todo, opens a draft PR and stops. It never merges. Each state change is committed to the
todos checkout (CLAUDE.md commit policy).

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
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from broker import todos as TODOS  # noqa: E402

PROJECTS_PER_TICK = 3
MAX_RUNNING = 3            # agents this tick has started that are still running (not manual ones)
SESSION_LIMIT_PCT = 80     # the 5-hour "Current session": start only while it is below this
WEEK_LIMIT_PCT = 75        # "Current week (all models)": refuse above this
SESSION_WINDOW = timedelta(hours=5)
NIGHT_START_HOUR = 22      # windows must begin at or after this local hour
LOCAL_TZ: tzinfo = ZoneInfo("America/Los_Angeles")  # the timezone the CLI reports reset times in
USAGE_LINE = re.compile(r"^(Current session|Current week \(all models\)):\s*(\d+)% used(.*)$", re.MULTILINE)
RESETS = re.compile(r"resets (.+?) \(")
# What an agent may do without a person watching. Everything else is refused in -p mode.
ALLOWED_TOOLS = ",".join([
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git:*)", "Bash(gh pr create:*)", "Bash(gh pr view:*)",
    "Bash(python3:*)", "Bash(python3 -m pytest:*)", "Bash(docker build:*)",
])


@dataclass
class Usage:
    session: int                     # percent of the current 5-hour window used
    week: int                        # percent of the week used
    session_resets: Optional[str]    # the CLI's reset time for the window, as printed, or None


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


def parse_usage(text: str) -> Usage:
    """The usage from `claude -p /usage`. Raises if the session or week figure is missing."""
    found: Dict[str, Tuple[int, str]] = {}
    for label, pct, rest in USAGE_LINE.findall(text):
        resets = RESETS.search(rest)
        found[label] = (int(pct), resets.group(1) if resets else None)
    if "Current session" not in found or "Current week (all models)" not in found:
        raise RuntimeError("the usage output has no session and week figures")
    session_pct, session_resets = found["Current session"]
    return Usage(session=session_pct, week=found["Current week (all models)"][0],
                 session_resets=session_resets)


def parse_reset(text: str, now: datetime) -> datetime:
    """'Nov 19, 11:19am' in the CLI's timezone, as an aware datetime near `now`."""
    try:
        parsed = datetime.strptime(text.strip(), "%b %d, %I:%M%p")
    except ValueError:
        raise RuntimeError(f"cannot read the reset time {text!r}")
    dt = parsed.replace(year=now.year, tzinfo=LOCAL_TZ)
    if dt < now - timedelta(days=180):   # a December reset read in January
        dt = dt.replace(year=now.year + 1)
    elif dt > now + timedelta(days=180):
        dt = dt.replace(year=now.year - 1)
    return dt


def window_start(usage: Usage, now: datetime) -> datetime:
    """When the current 5-hour window began. With no window running, it begins now."""
    if usage.session_resets is None:
        return now
    return parse_reset(usage.session_resets, now) - SESSION_WINDOW


def due(start: datetime, now: datetime, last_handled: Optional[int]) -> bool:
    """True for the first run of a window that began after NIGHT_START_HOUR, while it has begun
    and not been handled yet."""
    night = now.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)
    if night > now:
        night -= timedelta(days=1)
    return start >= night and start <= now and int(start.timestamp()) != last_handled


def usage_block(usage: Usage) -> Optional[str]:
    """Why no agent may start, or None when both figures are under their limits."""
    if usage.week > WEEK_LIMIT_PCT:
        return f"the week is at {usage.week}% (limit {WEEK_LIMIT_PCT}%)"
    if usage.session >= SESSION_LIMIT_PCT:
        return f"the current session is at {usage.session}% (it must be below {SESSION_LIMIT_PCT}%)"
    return None


def read_usage(claude_bin: str) -> Usage:
    """The subscription's usage, from the CLI. Any failure is an error, so nothing starts."""
    try:
        out = subprocess.run([claude_bin, "-p", "/usage", "--output-format", "text"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"claude /usage could not run: {e.__class__.__name__}")
    if out.returncode != 0:
        raise RuntimeError("claude /usage failed")
    return parse_usage(out.stdout)


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


def main(argv: Optional[List[str]] = None,
         usage: Optional[Callable[[], Usage]] = None,
         now: Optional[datetime] = None) -> int:
    ap = argparse.ArgumentParser(description="project tick")
    ap.add_argument("--config", required=True, type=Path)
    args = ap.parse_args(argv)
    raw = json.loads(args.config.read_text())
    todos_dir = Path(os.path.expanduser(raw["todosDir"]))
    todos_repo = Path(os.path.expanduser(raw["todosRepo"]))
    state_dir = Path(os.path.expanduser(raw.get("stateDir", "~/jobs/broker")))
    claude_bin = os.path.expanduser(raw.get("claudeBin", "~/.local/bin/claude"))
    usage_of = usage or (lambda: read_usage(claude_bin))
    now = now or datetime.now(LOCAL_TZ)
    repos: Dict[str, str] = raw.get("repos", {})
    dirs: Dict[str, str] = raw.get("projectDirs", {})

    # Once per window: the window must have begun after NIGHT_START_HOUR, and not been handled.
    tick_file = state_dir / "tick.json"
    tick_state = json.loads(tick_file.read_text()) if tick_file.exists() else {}
    try:
        usage_now = usage_of()
        start = window_start(usage_now, now)
    except RuntimeError as e:
        print(f"FAILED: cannot read the usage, so nothing is started: {e}")
        return 1
    if not due(start, now, tick_state.get("window")):
        return 0  # not this window; quiet, because cron runs this every ten minutes

    tick_state["window"] = int(start.timestamp())
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tick_file.write_text(json.dumps(tick_state))
    print(f"WINDOW {start.isoformat()}: session {usage_now.session}%, week {usage_now.week}%")

    why = usage_block(usage_now)
    if why:
        print(f"NOT STARTING: {why}")
        return 0

    projects: Dict[str, Tuple[Path, List[TODOS.Item]]] = {}
    for path in TODOS.project_files(todos_dir):
        projects[path.stem] = (path, TODOS.read_project(path))
    for line in close_merged(projects, repos, todos_repo):
        print(f"CLOSED {line}")

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

    agents_file.write_text(json.dumps(agents))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
