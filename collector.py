#!/usr/bin/env python3
"""jobs collector: reads Claude Code's files under ~/.claude and the hook spool,
and produces the snapshot { machine, projects, tasks } that the server shows.

Standard library only, and Python 3.9 syntax, so it runs on any machine's system
python3 with `python3 -I`. One file, because -I keeps the script's own directory
off sys.path. See DESIGN.md for the data sources and the status rules.

    python3 -I collector.py --config config/bix.json --out ~/jobs/snapshot/state.json
    python3 -I collector.py --config config/bix.json --once     # print one snapshot
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("collector")

SNAPSHOT_VERSION = 1
# The contract with the UI. Nothing else reaches the snapshot: adapters copy
# fields into these by name, never whole source records (job state.json carries
# providerEnv, which can hold credentials).
TASK_KEYS = (
    "id", "machineId", "projectId", "name", "kind", "status", "detail", "needs",
    "repo", "branch", "startedAt", "lastActivityAt", "costUsd", "tokens", "pid", "alive",
    "goal", "now", "blocked",
)
STATUSES = ("needs-you", "stalled", "working", "idle", "dead", "done")
KINDS = ("interactive", "bg")

SESSION_WORKING = {"busy", "running"}
SESSION_IDLE = {"idle"}
SESSION_NEEDS = {"needs_input", "waiting", "blocked"}
JOB_STATES = {"working", "blocked", "done"}

# Hook events (hook.py) and the status each implies, when newer than the session
# file's own status.
HOOK_STATUS = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PostToolUse": "working",
    "Stop": "idle",
    "Notification": "needs-you",
}
# Notification types that don't ask anything of the user.
QUIET_NOTIFICATIONS = {"idle_prompt", "auth_success"}
# A needs-you reason that means a usage or rate limit, not a question for you.
LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|resets? (at |in )?\d", re.IGNORECASE)
# Best-effort secret redaction for the text the viewport shows (workbench plan,
# decision 2). Applied to the whole string before any cut, so a cut can't split a secret.
REDACT_RES = [
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)(\s*[=:]\s*|\s+bearer\s+)\S+"),
     r"\1\2[redacted]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "[redacted]"),
    # Long runs that mix letters and digits (keys, hashes); plain slugs and paths pass.
    (re.compile(r"(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{40,}={0,2}"), "[redacted]"),
]
CONTEXT_TURNS = 6                       # the last few turns the viewport's Context shows
HEAD_READ = 64 * 1024                   # bytes read from the start, once, for the first prompt

# Transcript written this long after the session's last status change: it's working.
TRANSCRIPT_LEAD_MS = 10_000
TEXT_LIMITS = {"name": 120, "detail": 200, "needs": 300, "goal": 200, "now": 200, "turn": 600}
TRANSCRIPT_FIRST_READ = 256 * 1024     # bytes read from the end on first sight
TRANSCRIPT_MAX_READ = 4 * 1024 * 1024  # more new bytes than this: skip to the end


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- config

@dataclass
class ProjectRule:
    id: str
    name: str
    root: str          # absolute, no trailing slash
    repo: str          # "git" | "first-dir"


@dataclass
class Config:
    machine_id: str
    machine_label: str
    site: Optional[Dict[str, str]]
    claude_dir: Path
    spool_dir: Optional[Path]
    projects: List[ProjectRule]
    stalled_minutes: float = 10
    done_hide_minutes: float = 60
    dead_hide_minutes: float = 60
    job_max_age_hours: float = 168
    poll_seconds: float = 2
    heartbeat_seconds: float = 30
    context_dir: Optional[Path] = None  # per-session turn slices for the workbench viewport


def _expand(p: str) -> str:
    return os.path.normpath(os.path.expanduser(p))


def _num(raw: Dict[str, Any], key: str, default: float) -> float:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ConfigError(f"{key} must be a positive number")
    return float(v)


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    host = socket.gethostname()
    machine = raw.get("machine", {})
    if not isinstance(machine, dict):
        raise ConfigError("machine must be an object")
    site = raw.get("site")
    if site is not None and not (
        isinstance(site, dict) and isinstance(site.get("name"), str)
        and isinstance(site.get("url", ""), str)
    ):
        raise ConfigError("site must be {\"name\": str, \"url\": str}")
    rules = []
    for i, r in enumerate(raw.get("projects", [])):
        if not isinstance(r, dict) or not isinstance(r.get("match"), str) or not isinstance(r.get("name"), str):
            raise ConfigError(f"projects[{i}] needs string match and name")
        repo = r.get("repo", "git")
        if repo not in ("git", "first-dir"):
            raise ConfigError(f"projects[{i}].repo must be git or first-dir")
        pid = r.get("id", r["name"].lower())
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9-]+", pid):
            raise ConfigError(f"projects[{i}].id must be lowercase letters, digits, dashes")
        rules.append(ProjectRule(id=pid, name=r["name"], root=_expand(r["match"]).rstrip("/") or "/", repo=repo))
    if not rules:
        raise ConfigError("projects is empty: nothing would be shown")
    spool = raw.get("spoolDir")
    return Config(
        machine_id=str(machine.get("id", host)),
        machine_label=str(machine.get("label", machine.get("id", host))),
        site=site,
        claude_dir=Path(_expand(raw.get("claudeDir", "~/.claude"))),
        spool_dir=Path(_expand(spool)) if spool else None,
        projects=rules,
        stalled_minutes=_num(raw, "stalledMinutes", 10),
        done_hide_minutes=_num(raw, "doneHideMinutes", 60),
        dead_hide_minutes=_num(raw, "deadHideMinutes", 60),
        job_max_age_hours=_num(raw, "jobMaxAgeHours", 168),
        poll_seconds=_num(raw, "pollSeconds", 2),
        heartbeat_seconds=_num(raw, "heartbeatSeconds", 30),
        context_dir=Path(_expand(raw["contextDir"])) if raw.get("contextDir") else None,
    )


# ---------------------------------------------------------------- helpers

def now_ms() -> int:
    return int(time.time() * 1000)


def iso_ms(v: Any) -> Optional[int]:
    """ISO-8601 string ('…Z' included, which 3.9's fromisoformat refuses) to epoch ms."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def int_ms(v: Any) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def text(v: Any, key: str) -> Optional[str]:
    """One line of model-written text, redacted, collapsed and cut to TEXT_LIMITS[key]."""
    if not isinstance(v, str):
        return None
    v = " ".join(redact(v).split())
    if not v:
        return None
    limit = TEXT_LIMITS[key]
    return v if len(v) <= limit else v[: limit - 1] + "…"


def map_project(cwd: str, rules: List[ProjectRule]) -> Optional[Tuple[ProjectRule, Optional[str]]]:
    """First rule whose root contains cwd, and the repo label for the tile."""
    cwd = os.path.normpath(cwd)
    for rule in rules:
        if cwd == rule.root or cwd.startswith(rule.root.rstrip("/") + "/"):
            rel = cwd[len(rule.root):].strip("/")
            if rule.repo == "first-dir":
                return rule, (rel.split("/")[0] if rel else None)
            return rule, git_repo_label(cwd, rule.root)
    return None


def git_repo_label(cwd: str, stop: str) -> Optional[str]:
    """Name of the nearest directory at or above cwd (not above `stop`) holding a
    .git entry. A worktree's .git is a file, so a worktree gives its own slug."""
    d = cwd
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d) or None
        if d == stop or d == os.path.dirname(d):
            return None
        d = os.path.dirname(d)


class JsonFiles:
    """Parsed JSON files, re-read only when mtime or size changes. A file that
    fails to parse is retried next cycle (it may be mid-write) and reported only
    if it fails twice unchanged."""

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[int, int, Any, int]] = {}

    def read(self, path: Path) -> Tuple[Optional[Any], Optional[str]]:
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            self._cache.pop(str(path), None)
            return None, None
        key = str(path)
        hit = self._cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            if hit[3] == 0:
                return hit[2], None
            fails = hit[3] + 1
        else:
            fails = 0
        try:
            data = json.loads(path.read_bytes())
        except FileNotFoundError:
            self._cache.pop(key, None)
            return None, None
        except (OSError, ValueError) as e:
            prev = hit[2] if hit else None
            self._cache[key] = (st.st_mtime_ns, st.st_size, prev, max(fails, 1))
            return prev, (f"cannot parse {path.name}: {e}" if fails >= 1 else None)
        self._cache[key] = (st.st_mtime_ns, st.st_size, data, 0)
        return data, None

    def forget_missing(self, keep: set) -> None:
        for k in [k for k in self._cache if k not in keep]:
            del self._cache[k]


# ---------------------------------------------------------------- processes

def own_pid_domain() -> Optional[str]:
    """Same shape as Claude Code's pidDomain on Linux: linux:<machine-id>:pid:[<ns inode>]."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        mid = Path("/etc/machine-id").read_text().strip()
        return f"linux:{mid}:{os.readlink('/proc/self/ns/pid')}"
    except OSError:
        return None


def proc_start(pid: int) -> Optional[str]:
    """Linux process start time in clock ticks (/proc/<pid>/stat field 22), which
    is what Claude Code records as procStart. None if the process is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm (field 2) may contain spaces and ')': split after the last ')'.
    rest = raw.rsplit(")", 1)[1].split()
    return rest[19] if len(rest) > 19 else None


def pid_alive(pid: int, expected_start: Optional[str]) -> bool:
    if sys.platform.startswith("linux"):
        started = proc_start(pid)
        if started is None:
            return False
        return expected_start is None or started == str(expected_start)
    # Elsewhere (macOS): existence only. Claude Code's procStart format there is
    # unverified, so pid reuse is not detected.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- transcripts

@dataclass
class TailInfo:
    branch: Optional[str] = None
    cost_usd: Optional[float] = None
    mtime_ms: Optional[int] = None
    goal: Optional[str] = None      # the first real prompt, from the head of the file
    title: Optional[str] = None     # the session's ai-title
    now: Optional[str] = None       # the latest assistant text
    turns: List[Dict[str, Any]] = field(default_factory=list)  # the last few turns


def redact(s: str) -> str:
    for rx, repl in REDACT_RES:
        s = rx.sub(repl, s)
    return s


def blocked_view(status: str, needs: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
    """What the viewport's Blocked line says: on you, on a limit, or on something else.
    `needs` is already cut and redacted by the caller."""
    if status == "needs-you":
        on = "limit" if needs and LIMIT_RE.search(needs) else "you"
        return {"on": on, "why": needs}
    if status == "stalled":
        return {"on": "other", "why": "stalled, unknown"}
    return None


def turn_text(s: str) -> str:
    """A transcript turn for the Context slice: line breaks kept, cut at the turn limit."""
    s = s.strip()
    limit = TEXT_LIMITS["turn"]
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _record(line: bytes) -> Optional[Dict[str, Any]]:
    if not line.strip():
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None  # a torn line where a read starts or stops; the next records carry the same fields
    return rec if isinstance(rec, dict) else None


def _message_text(rec: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(role, text) for a user or assistant turn with text in it, else None. Tool
    calls and tool results carry no text here, so they never reach the viewport."""
    kind = rec.get("type")
    if kind not in ("user", "assistant"):
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        body = "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    else:
        return None
    body = body.strip()
    # Wrappers such as <system-reminder> and <command-name> aren't what the person said.
    if not body or body.startswith("<"):
        return None
    return kind, redact(body)


def first_prompt(path: Path) -> Optional[str]:
    """The session's first real prompt, read once from the head of its transcript."""
    with path.open("rb") as f:
        head = f.read(HEAD_READ)
    for line in head.split(b"\n"):
        rec = _record(line)
        mt = _message_text(rec) if rec else None
        if mt and mt[0] == "user":
            return mt[1]
    return None


class Transcripts:
    """Reads only the bytes appended to each transcript since the last poll."""

    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = projects_dir
        self._paths: Dict[str, Path] = {}
        self._state: Dict[str, Tuple[int, int, bytes, TailInfo]] = {}  # path -> inode, offset, partial, info

    def path_for(self, session_id: str, cwd: str) -> Optional[Path]:
        p = self._paths.get(session_id)
        if p and p.exists():
            return p
        guess = self.projects_dir / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
        if guess.exists():
            self._paths[session_id] = guess
            return guess
        found = next(iter(self.projects_dir.glob(f"*/{session_id}.jsonl")), None)
        if found:
            self._paths[session_id] = found
        return found

    def read(self, path: Path) -> TailInfo:
        st = path.stat()
        key = str(path)
        prev = self._state.get(key)
        if prev is None or prev[0] != st.st_ino or st.st_size < prev[1]:
            offset = max(0, st.st_size - TRANSCRIPT_FIRST_READ)
            partial, info, skip_first = b"", TailInfo(), offset > 0
            info.goal = text(first_prompt(path), "goal")
        else:
            _, offset, partial, info = prev
            skip_first = False
            if st.st_size - offset > TRANSCRIPT_MAX_READ:
                offset, partial, skip_first = st.st_size - TRANSCRIPT_FIRST_READ, b"", True
        info.mtime_ms = st.st_mtime_ns // 1_000_000
        if st.st_size > offset:
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(st.st_size - offset)
            data = partial + chunk
            lines = data.split(b"\n")
            partial = lines.pop()  # incomplete last line, finished next time
            if skip_first and lines:
                lines.pop(0)  # started mid-line
            for line in lines:
                self._scan(line, info)
            offset = st.st_size
        self._state[key] = (st.st_ino, offset, partial, info)
        return info

    @staticmethod
    def _scan(line: bytes, info: TailInfo) -> None:
        rec = _record(line)
        if rec is None:
            return
        mt = _message_text(rec)
        if mt is not None:
            role, body = mt
            if role == "user" and info.goal is None:
                info.goal = text(body, "goal")
            if role == "assistant":
                info.now = text(body, "now")
            info.turns.append({"role": role, "at": iso_ms(rec.get("timestamp")), "text": turn_text(body)})
            del info.turns[:-CONTEXT_TURNS]
        if rec.get("type") == "ai-title" and isinstance(rec.get("aiTitle"), str):
            info.title = text(redact(rec["aiTitle"]), "name")
        if rec.get("type") == "cost-state":
            cost = rec.get("totalCostUSD")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                info.cost_usd = round(float(cost), 4)
        branch = rec.get("gitBranch")
        if isinstance(branch, str) and branch and branch != "HEAD":
            info.branch = branch


# ---------------------------------------------------------------- hooks

@dataclass
class HookEvent:
    event: str
    at: int
    notification_type: Optional[str] = None
    message: Optional[str] = None


class HookSpool:
    """One JSON file per hook event, written by hook.py. Read, applied, deleted."""

    def __init__(self, spool_dir: Optional[Path]) -> None:
        self.dir = spool_dir
        self.latest: Dict[str, HookEvent] = {}

    def drain(self) -> List[str]:
        if self.dir is None:
            return []
        problems = []
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".json"))
        except OSError as e:
            return [f"cannot read hook spool: {e.strerror}"]
        for name in names:
            path = self.dir / name
            try:
                rec = json.loads(path.read_bytes())
                sid, event, at = rec["sessionId"], rec["event"], rec["at"]
                if not (isinstance(sid, str) and isinstance(event, str) and isinstance(at, int)):
                    raise ValueError("wrong field types")
            except (OSError, ValueError, KeyError, TypeError) as e:
                problems.append(f"bad hook event {name}: {e}")
                rec = None
            try:
                path.unlink()
            except OSError as e:
                problems.append(f"cannot remove hook event {name}: {e.strerror}")
            if rec is None:
                continue
            if event == "SessionEnd":
                self.latest.pop(sid, None)
                continue
            if event not in HOOK_STATUS:
                continue
            cur = self.latest.get(sid)
            if cur is None or at >= cur.at:
                self.latest[sid] = HookEvent(
                    event=event, at=at,
                    notification_type=rec.get("notificationType") if isinstance(rec.get("notificationType"), str) else None,
                    message=rec.get("message") if isinstance(rec.get("message"), str) else None,
                )
        return problems

    def prune(self, older_than_ms: int, keep: set) -> None:
        for sid in [s for s, e in self.latest.items() if e.at < older_than_ms and s not in keep]:
            del self.latest[sid]


# ---------------------------------------------------------------- collector

@dataclass
class _Draft:
    id: str
    cwd: str
    kind: str
    name: Optional[str] = None
    started_at: Optional[int] = None
    pid: Optional[int] = None
    alive: Optional[bool] = None
    session_status: Optional[str] = None
    session_status_at: Optional[int] = None
    session_updated_at: Optional[int] = None
    job: Optional[Dict[str, Any]] = None
    job_updated_at: Optional[int] = None
    warnings: List[str] = field(default_factory=list)


class Collector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sessions_dir = cfg.claude_dir / "sessions"
        self.jobs_dir = cfg.claude_dir / "jobs"
        self.files = JsonFiles()
        self.transcripts = Transcripts(cfg.claude_dir / "projects")
        self.hooks = HookSpool(cfg.spool_dir)
        self.pid_domain = own_pid_domain()
        self.versions: set = set()
        self.context_written: Dict[str, int] = {}  # session id -> transcript mtime last written

    # Sources ------------------------------------------------------------

    def _sessions(self, warnings: List[str], seen: set) -> Dict[str, _Draft]:
        out: Dict[str, _Draft] = {}
        try:
            names = [n for n in os.listdir(self.sessions_dir) if n.endswith(".json")]
        except FileNotFoundError:
            return out
        except OSError as e:
            warnings.append(f"cannot list sessions: {e.strerror}")
            return out
        for name in names:
            path = self.sessions_dir / name
            seen.add(str(path))
            d, err = self.files.read(path)
            if err:
                warnings.append(f"session {err}")
            if not isinstance(d, dict):
                continue
            version = d.get("version")
            if isinstance(version, str):
                self.versions.add(version)
            domain = d.get("pidDomain")
            if self.pid_domain and isinstance(domain, str) and domain != self.pid_domain:
                continue  # another pid namespace (a container, another boot): not ours to judge
            pid, sid, cwd, status = d.get("pid"), d.get("sessionId"), d.get("cwd"), d.get("status")
            missing = [k for k, v in (("pid", pid), ("sessionId", sid), ("cwd", cwd), ("status", status))
                       if v is None]
            if missing or not (isinstance(pid, int) and isinstance(sid, str) and isinstance(cwd, str)
                               and isinstance(status, str)):
                warnings.append(f"session file format changed: {name} lacks or mistypes "
                                f"{', '.join(missing) or 'a field'} (Claude Code {version or 'unknown'})")
                continue
            kind = d.get("kind")
            if kind not in KINDS:
                warnings.append(f"unknown session kind {str(kind)[:20]!r} (Claude Code {version or 'unknown'})")
                kind = "interactive"
            if status not in SESSION_WORKING | SESSION_IDLE | SESSION_NEEDS:
                warnings.append(f"unknown session status {status[:20]!r} (Claude Code {version or 'unknown'})")
            ps = d.get("procStart")
            draft = _Draft(
                id=sid, cwd=cwd, kind=kind, name=d.get("name"),
                started_at=int_ms(d.get("startedAt")), pid=pid,
                alive=pid_alive(pid, str(ps) if ps is not None else None),
                session_status=status,
                session_status_at=int_ms(d.get("statusUpdatedAt")),
                session_updated_at=int_ms(d.get("updatedAt")) or int_ms(d.get("statusUpdatedAt")),
            )
            prev = out.get(sid)
            # A resumed session can leave an older file behind under a dead pid.
            if prev is None or (draft.alive and not prev.alive):
                out[sid] = draft
        return out

    def _jobs(self, warnings: List[str], seen: set) -> List[Dict[str, Any]]:
        out = []
        try:
            ids = os.listdir(self.jobs_dir)
        except FileNotFoundError:
            return out
        except OSError as e:
            warnings.append(f"cannot list jobs: {e.strerror}")
            return out
        for jid in ids:
            if not (self.jobs_dir / jid).is_dir():
                continue  # jobs/ also holds files such as pins.json
            path = self.jobs_dir / jid / "state.json"
            seen.add(str(path))
            d, err = self.files.read(path)
            if err:
                warnings.append(f"job {err}")
            if not isinstance(d, dict):
                continue
            state, sid, cwd = d.get("state"), d.get("sessionId"), d.get("cwd")
            if not (isinstance(sid, str) and isinstance(cwd, str) and isinstance(state, str)):
                warnings.append(f"job file format changed: {jid}/state.json lacks state, sessionId or cwd "
                                f"(Claude Code {d.get('cliVersion') or 'unknown'})")
                continue
            if state not in JOB_STATES:
                warnings.append(f"unknown job state {state[:20]!r} (Claude Code {d.get('cliVersion') or 'unknown'})")
            tokens = d.get("tokens")
            # Copy by name only: never the whole record.
            out.append({
                "sessionId": sid, "cwd": cwd, "state": state,
                "name": d.get("name"), "detail": d.get("detail"), "needs": d.get("needs"),
                "tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
                "createdAt": iso_ms(d.get("createdAt")),
                "updatedAt": iso_ms(d.get("updatedAt")),
            })
        return out

    # Derivation -----------------------------------------------------------

    def _status(self, t: _Draft, hook: Optional[HookEvent], last_activity: Optional[int],
                now: int, transcript_ms: Optional[int] = None) -> Tuple[str, Optional[str]]:
        """(status, needs). The UI reads only these, never the raw fields."""
        if t.alive is False:
            return "dead", None
        status, needs = None, None
        if t.job is not None:
            state = t.job["state"]
            if state == "blocked":
                status, needs = "needs-you", text(t.job.get("needs"), "needs")
            elif state == "done":
                status = "done"
            elif state == "working":
                status = "working"
        if status is None:
            hook_newer = hook is not None and (t.session_status_at is None or hook.at > t.session_status_at)
            if hook_newer:
                status = HOOK_STATUS[hook.event]
                if hook.event == "Notification":
                    if hook.notification_type in QUIET_NOTIFICATIONS:
                        status = "idle"
                    else:
                        needs = text(hook.message, "needs")
            elif t.session_status in SESSION_NEEDS:
                status = "needs-you"
            elif t.session_status in SESSION_WORKING:
                status = "working"
            elif (transcript_ms is not None and t.session_status_at is not None
                  and transcript_ms - t.session_status_at > TRANSCRIPT_LEAD_MS):
                # The session file's status can lag (seen on 2.1.327: "idle" mid-turn).
                # An idle session doesn't write its transcript, so this one is working.
                status = "working"
            else:
                status = "idle"
        if status == "working" and last_activity is not None:
            if now - last_activity > self.cfg.stalled_minutes * 60_000:
                status = "stalled"
        return status, needs

    def _context_stale(self, sid: str, mtime_ms: Optional[int]) -> bool:
        # Session ids become file names: refuse anything that could be a path.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
            return False
        return mtime_ms is not None and self.context_written.get(sid) != mtime_ms

    def _prune_context(self, live: set, warnings: List[str]) -> None:
        """Delete slices of tasks no longer shown, so the directory holds only what the page can ask for."""
        ctx = self.cfg.context_dir
        try:
            names = os.listdir(ctx)
        except FileNotFoundError:
            return  # created by install-host.sh; the write above will say so
        except OSError as e:
            warnings.append(f"cannot list context slices: {e.strerror}")
            return
        for name in names:
            if name.endswith(".json") and name[:-5] not in live:
                try:
                    os.unlink(ctx / name)
                except OSError as e:
                    warnings.append(f"cannot remove a context slice: {e.strerror}")
        self.context_written = {k: v for k, v in self.context_written.items() if k in live}

    def snapshot(self) -> Dict[str, Any]:
        now = now_ms()
        warnings: List[str] = []
        seen: set = set()
        warnings.extend(self.hooks.drain())
        drafts = self._sessions(warnings, seen)
        max_job_age = self.cfg.job_max_age_hours * 3_600_000
        for job in self._jobs(warnings, seen):
            t = drafts.get(job["sessionId"])
            if t is None:
                if job["updatedAt"] is None or now - job["updatedAt"] > max_job_age:
                    continue
                t = drafts[job["sessionId"]] = _Draft(
                    id=job["sessionId"], cwd=job["cwd"], kind="bg", started_at=job["createdAt"])
            prev_at = t.job_updated_at or 0
            if t.job is None or (job["updatedAt"] or 0) >= prev_at:
                t.job, t.job_updated_at = job, job["updatedAt"]
        self.files.forget_missing(seen)

        tasks = []
        for t in drafts.values():
            mapped = map_project(t.cwd, self.cfg.projects)
            if mapped is None:
                continue
            rule, repo = mapped
            tail = TailInfo()
            tpath = self.transcripts.path_for(t.id, t.cwd)
            if tpath is not None:
                try:
                    tail = self.transcripts.read(tpath)
                except OSError as e:
                    warnings.append(f"cannot read a transcript: {e.strerror}")
            hook = self.hooks.latest.get(t.id)
            last_activity = max(
                [v for v in (tail.mtime_ms, t.session_status_at, t.job_updated_at,
                             hook.at if hook else None) if v is not None] or [0]) or None
            status, needs = self._status(t, hook, last_activity, now, tail.mtime_ms)
            age = now - (last_activity or t.session_updated_at or now)
            if status == "done" and age > self.cfg.done_hide_minutes * 60_000:
                continue
            if status == "dead" and age > self.cfg.dead_hide_minutes * 60_000:
                continue
            job = t.job or {}
            if self.cfg.context_dir is not None and tail.turns and self._context_stale(t.id, tail.mtime_ms):
                try:
                    write_snapshot(self.cfg.context_dir / f"{t.id}.json", {
                        "sessionId": t.id, "updatedAt": tail.mtime_ms, "turns": tail.turns})
                    self.context_written[t.id] = tail.mtime_ms
                except OSError as e:
                    warnings.append(f"cannot write a context slice: {e.strerror}")
            tasks.append({
                "id": t.id,
                "machineId": self.cfg.machine_id,
                "projectId": rule.id,
                "name": text(job.get("name"), "name") or text(t.name, "name") or (repo or rule.name),
                "kind": t.kind,
                "status": status,
                "detail": text(job.get("detail"), "detail"),
                "needs": needs,
                "repo": repo,
                "branch": tail.branch,
                "startedAt": t.started_at,
                "lastActivityAt": last_activity,
                "costUsd": tail.cost_usd,
                "tokens": job.get("tokens"),
                "pid": t.pid,
                "alive": t.alive,
                "goal": text(job.get("name"), "goal") or text(tail.title, "goal") or text(tail.goal, "goal"),
                "now": text(job.get("detail"), "now") or text(tail.now, "now"),
                "blocked": blocked_view(status, needs),
            })
        alive_ids = {t.id for t in drafts.values() if t.alive}
        if self.cfg.context_dir is not None:
            self._prune_context({t["id"] for t in tasks}, warnings)
        self.hooks.prune(now - 24 * 3_600_000, alive_ids)
        order = {s: i for i, s in enumerate(STATUSES)}
        tasks.sort(key=lambda x: (order[x["status"]], -(x["lastActivityAt"] or 0)))
        return {
            "version": SNAPSHOT_VERSION,
            "machine": {"id": self.cfg.machine_id, "label": self.cfg.machine_label},
            "site": self.cfg.site,
            "generatedAt": now,
            "claudeVersions": sorted(self.versions),
            "projects": [{"id": r.id, "name": r.name} for r in self.cfg.projects],
            "tasks": tasks,
            "warnings": sorted(set(warnings)),
        }


# ---------------------------------------------------------------- running

def write_snapshot(path: Path, snap: Dict[str, Any]) -> None:
    """Atomic: the server never sees a half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def run_forever(cfg: Config, sink: Callable[[Dict[str, Any]], None],
                stop: Optional[Callable[[], bool]] = None) -> None:
    """Collect every poll_seconds; call sink when the snapshot changes, and at
    least every heartbeat_seconds so the server can tell the collector is alive."""
    c = Collector(cfg)
    last_body, last_sent = None, 0.0
    logged_warnings: set = set()
    failing: Optional[str] = None
    versions_logged: set = set()
    log.info("collector started: machine %s, %d project rule(s), spool %s",
             cfg.machine_id, len(cfg.projects), "on" if cfg.spool_dir else "off")
    while not (stop and stop()):
        try:
            snap = c.snapshot()
            body = json.dumps({k: v for k, v in snap.items() if k != "generatedAt"}, sort_keys=True)
            if body != last_body or time.monotonic() - last_sent >= cfg.heartbeat_seconds:
                sink(snap)
                last_body, last_sent = body, time.monotonic()
            if failing:
                log.info("collector recovered")
                failing = None
        except Exception as e:  # deliberate: keep polling; the stale snapshot surfaces this in the UI too
            msg = f"{type(e).__name__}: {e}"
            if msg != failing:
                log.exception("collect FAILED")
                failing = msg
            snap = None
        if snap is not None:
            current = set(snap["warnings"])
            for w in sorted(current - logged_warnings):
                log.warning("%s", w)
            for w in sorted(logged_warnings - current):
                log.info("cleared: %s", w)
            logged_warnings = current
            new_versions = set(snap["claudeVersions"]) - versions_logged
            if new_versions:
                log.info("Claude Code versions seen: %s", ", ".join(sorted(snap["claudeVersions"])))
                versions_logged |= new_versions
        time.sleep(cfg.poll_seconds)


def setup_logging() -> None:
    logging.Formatter.converter = time.gmtime
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s collector %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path, help="snapshot file to keep up to date")
    g.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = ap.parse_args(argv)
    setup_logging()
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.critical("%s", e)
        return 2
    if args.once:
        cfg.spool_dir = None  # never consume hook events the running collector is owed
        json.dump(Collector(cfg).snapshot(), sys.stdout, indent=2)
        print()
        return 0
    out: Path = args.out.expanduser()

    def sink(snap: Dict[str, Any]) -> None:
        try:
            write_snapshot(out, snap)
        except OSError as e:
            raise RuntimeError(f"snapshot write to {out} FAILED: {e.strerror}") from e

    run_forever(cfg, sink)
    return 0


if __name__ == "__main__":
    sys.exit(main())
