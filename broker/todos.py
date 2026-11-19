"""Todo files: one markdown file per project (bix-infra/todos/<project>.md).

Each item is a line `- [N] text`, with optional indented continuation lines (Why, Notes)
that belong to the item above. N is the state: 0 unapproved, 1 approved, 2 in progress,
3 done. The legacy checkboxes read as `[ ]` = 0 and `[x]` = 3. An item is rewritten to
the integer form only when its state changes; everything else in the file is kept.

Writes are compare-and-set: the caller names the state it saw, and a stale view is refused.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

STATES = {0: "unapproved", 1: "approved", 2: "in progress", 3: "done"}
ALLOWED_MOVES = {(0, 1), (1, 2), (2, 1), (2, 3)}
ITEM = re.compile(r"^- \[( |x|[0-3])\] (.*)$")
LEGACY = {" ": 0, "x": 3}
SKIP_FILES = {"GUIDE.md", "ALL.md"}


class TodoError(ValueError):
    """A refusal the caller can show: a stale state, an unknown item, or a move not allowed."""


@dataclass
class Item:
    id: str
    state: int
    text: str
    section: str
    line: int          # index into the file's lines
    legacy: bool


def _ids(texts: List[str]) -> List[str]:
    """Stable ids from the text, with a suffix when two items share one."""
    seen: Dict[str, int] = {}
    out = []
    for t in texts:
        base = hashlib.sha1(t.encode()).hexdigest()[:10]
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base}-{n}")
    return out


def parse(text: str) -> List[Item]:
    lines = text.split("\n")
    found: List[tuple] = []
    section = ""
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = ITEM.match(line)
        if m:
            tag, body = m.group(1), m.group(2).strip()
            legacy = tag in LEGACY
            state = LEGACY[tag] if legacy else int(tag)
            found.append((state, body, section, i, legacy))
    ids = _ids([body for _, body, _, _, _ in found])
    return [Item(id=ids[k], state=s, text=b, section=sec, line=i, legacy=leg)
            for k, (s, b, sec, i, leg) in enumerate(found)]


def set_state(text: str, item_id: str, expected: int, to: int) -> str:
    """The file with one item moved from `expected` to `to`. Raises TodoError otherwise."""
    if to not in STATES:
        raise TodoError("state must be one of 0, 1, 2, 3")
    items = [it for it in parse(text) if it.id == item_id]
    if not items:
        raise TodoError("no such todo (it may have been edited or moved)")
    item = items[0]
    if item.state != expected:
        raise TodoError(f"that todo is {STATES[item.state]}, not {STATES[expected]}; reload and try again")
    if (expected, to) not in ALLOWED_MOVES:
        raise TodoError(f"a todo can't go from {STATES[expected]} to {STATES[to]}")
    lines = text.split("\n")
    old = lines[item.line]
    m = ITEM.match(old)
    lines[item.line] = f"- [{to}] {m.group(2)}"
    return "\n".join(lines)


def read_project(path: Path) -> List[Item]:
    return parse(path.read_text())


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def project_files(todos_dir: Path) -> List[Path]:
    return sorted(p for p in todos_dir.glob("*.md") if p.name not in SKIP_FILES)


class CommitError(RuntimeError):
    """A todo change was written but not committed or pushed. The caller reports it."""


def commit_file(repo: Path, path: Path, message: str) -> None:
    """Commit one todo file on the repo's current branch and push it (CLAUDE.md commit
    policy: short messages, no author mention). Only that file is staged, so other local
    changes in the checkout stay where they are."""
    rel = str(path.relative_to(repo))
    steps = [["git", "add", "--", rel],
             ["git", "commit", "-q", "-m", message, "--", rel],
             ["git", "push", "-q"]]
    for cmd in steps:
        try:
            out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CommitError(f"{cmd[1]} could not run: {e.__class__.__name__}")
        if out.returncode != 0:
            first = (out.stderr.strip().splitlines() or [f"{cmd[1]} failed"])[0]
            raise CommitError(f"{cmd[1]} failed: {first[:200]}")
