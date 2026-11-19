#!/usr/bin/env python3
"""jobs broker (workbench plan, phase 3): drives Claude Code sessions for the web.

Runs on the host as matt, because the sessions it starts are full Claude Code
sessions with matt's files and settings. Its only door is a unix socket: no TCP
listener. Every web action carries the visitor's Access JWT, which the broker
verifies itself (signature, audience, expiry), so it doesn't trust the container.

One driver per session. The web driver is one `claude -p` stream-json process per
session; a terminal session is held by its own process, and `take_over` stops that
process before the web resumes it. Permission prompts go to approval.py, which
asks the broker over this socket and waits for the decision.

Run: python3 -I broker/broker.py --config <path>    (see DESIGN-style notes in
jobs/CLAUDE.md). Stdlib plus PyJWT, which the host already has.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # collector.py: redact(), text(), pid_alive()
import collector  # noqa: E402
from broker import todos as TODOS  # noqa: E402  (the package is importable from the repo root inserted above)

log = logging.getLogger("broker")

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MODEL = "opus"  # every session the workbench starts: the most capable model on the subscription
MAX_REQUEST = 64 * 1024
TRANSCRIPT_QUIET_S = 10.0
HISTORY_BYTES = 2 * 1024 * 1024  # the end of the transcript that history reads
HISTORY_ITEMS = 300
HISTORY_TEXT = 4000              # per message, in the Open view
PR_CACHE_S = 60             # how long a repo's PR list is reused
PR_MERGED_DAYS = 14         # merged PRs older than this leave the board
MAX_TEXT = 20_000
APPROVAL_TEXT = 300
APPROVAL_TOOL = "mcp__jobs-approval__approve"
APPROVAL_SERVER = Path(__file__).resolve().parent / "approval.py"


class BrokerError(Exception):
    """A refusal the web can show. `status` is an HTTP-style code for the proxy."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Settings:
    socket_path: Path
    approval_path: Path
    state_dir: Path
    claude_bin: str
    claude_home: Path
    roots: List[Path]
    aud: str
    team: str
    max_drivers: int = 4
    approval_timeout: float = 600.0
    ring: int = 2000
    terminal_stop_wait: float = 15.0
    release_wait: float = 30.0
    todos_dir: Optional[Path] = None   # bix-infra/todos: one <project>.md per project
    repos: Dict[str, str] = field(default_factory=dict)  # project name -> GitHub owner/repo


@dataclass
class Approval:
    id: str
    tool: str
    summary: str
    decided: threading.Event = field(default_factory=threading.Event)
    allow: bool = False


class Ring:
    """A session's recent events, numbered. The numbers keep counting across driver
    restarts, so a client's cursor stays valid after a stop and a resume."""

    def __init__(self, size: int) -> None:
        self.lock = threading.Lock()
        self.events: Deque[Tuple[int, Dict[str, Any]]] = deque(maxlen=size)
        self.seq = 0

    def add(self, ev: Dict[str, Any]) -> None:
        with self.lock:
            self.seq += 1
            self.events.append((self.seq, ev))

    def since(self, after: int) -> Tuple[List[Dict[str, Any]], bool]:
        """Events after `after`, and whether some were already dropped from the ring."""
        with self.lock:
            oldest = self.events[0][0] if self.events else self.seq + 1
            gap = after + 1 < oldest
            out = [dict(ev, seq=s) for s, ev in self.events if s > after]
        return out, gap


class Session:
    """One `claude -p` stream-json process for one session."""

    def __init__(self, sid: str, cwd: str, model: str, broker: "Broker", resume: bool) -> None:
        self.sid = sid
        self.cwd = cwd
        self.model = model
        self.broker = broker
        self.ticket = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.ring = broker.ring_for(sid)  # kept by the broker, so the sequence outlives this process
        self.busy = False
        self.approvals: Dict[str, Approval] = {}
        self.seen_clients: Deque[str] = deque(maxlen=200)
        self.mcp_path = broker.cfg.state_dir / "run" / f"{self.ticket}.json"
        self.proc = self._spawn(resume)
        self.reader = threading.Thread(target=self._read, daemon=True, name=f"read-{sid[:8]}")
        self.reader.start()

    def _spawn(self, resume: bool) -> subprocess.Popen:
        self.mcp_path.parent.mkdir(mode=0o700, exist_ok=True)
        cfg = {"mcpServers": {"jobs-approval": {
            "command": sys.executable,
            "args": ["-I", str(APPROVAL_SERVER), "--socket", str(self.broker.cfg.approval_path),
                     "--ticket", self.ticket]}}}
        fd = os.open(self.mcp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        args = [self.broker.cfg.claude_bin, "-p", "--model", self.model,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose",
                "--permission-prompt-tool", APPROVAL_TOOL, "--permission-prompts", "host",
                "--mcp-config", str(self.mcp_path)]
        args += ["--resume", self.sid] if resume else ["--session-id", self.sid]
        return subprocess.Popen(args, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

    # Event ring -----------------------------------------------------------

    def add(self, ev: Dict[str, Any]) -> None:
        self.ring.add(ev)

    # Reading the process --------------------------------------------------

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            for ev in shape(msg):
                self.add(ev)
                if ev["kind"] == "turn_end":
                    with self.lock:
                        self.busy = False
        self.add({"kind": "driver", "state": "ended"})
        with self.lock:
            self.busy = False
        self.broker.forget(self)

    # Actions --------------------------------------------------------------

    def send(self, text: str, client_id: str) -> Dict[str, Any]:
        with self.lock:
            if client_id in self.seen_clients:
                return {"duplicate": True}
            if self.busy:
                raise BrokerError("this session is still answering; wait for it to finish", 409)
            if self.proc.poll() is not None or self.proc.stdin is None:
                raise BrokerError("the session's process has ended", 409)
            payload = {"type": "user", "message": {"role": "user", "content": text}}
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise BrokerError(f"cannot write to the session: {e.__class__.__name__}", 500)
            self.busy = True
            self.seen_clients.append(client_id)
        self.add({"kind": "user", "text": collector.redact(text)})
        return {"duplicate": False}

    def request_approval(self, tool: str, summary: str) -> bool:
        ap = Approval(id=uuid.uuid4().hex[:12], tool=tool, summary=collector.text(collector.redact(summary), "needs") or tool)
        with self.lock:
            self.approvals[ap.id] = ap
        self.add({"kind": "approval", "id": ap.id, "tool": ap.tool, "summary": ap.summary})
        if not ap.decided.wait(self.broker.cfg.approval_timeout):
            self.add({"kind": "approval_decided", "id": ap.id, "allow": False, "reason": "timed out"})
            with self.lock:
                self.approvals.pop(ap.id, None)
            return False
        return ap.allow

    def decide(self, approval_id: str, allow: bool) -> None:
        with self.lock:
            ap = self.approvals.pop(approval_id, None)
        if ap is None:
            raise BrokerError("no such approval (it may have timed out)", 404)
        ap.allow = allow
        ap.decided.set()
        self.add({"kind": "approval_decided", "id": approval_id, "allow": allow})

    def pending(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [{"id": a.id, "tool": a.tool, "summary": a.summary} for a in self.approvals.values()]

    def stop(self, wait: float) -> None:
        """End the process. The conversation stays on disk and the next send resumes it."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def release(self) -> None:
        """Close stdin so the process finishes its turn and exits."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=self.broker.cfg.release_wait)
        except subprocess.TimeoutExpired:
            self.stop(5)


def shape(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What the web gets from one stream-json message. Only text, tool names and
    outcomes; tool inputs and outputs stay on the host."""
    kind = msg.get("type")
    out: List[Dict[str, Any]] = []
    if kind == "stream_event":
        ev = msg.get("event") or {}
        delta = ev.get("delta") or {}
        if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta" \
                and isinstance(delta.get("text"), str):
            out.append({"kind": "delta", "text": collector.redact(delta["text"])})
    elif kind == "assistant":
        for block in (msg.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append({"kind": "text", "text": collector.redact(block["text"])})
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                out.append({"kind": "tool", "name": str(block.get("name"))[:80]})
    elif kind == "user":
        for block in (msg.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append({"kind": "tool_result", "error": bool(block.get("is_error"))})
    elif kind == "result":
        out.append({"kind": "turn_end", "ok": not msg.get("is_error")})
    return out


def exited(pid: int) -> bool:
    """True once the process is gone or is a zombie (it has exited; its parent hasn't reaped it yet)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state in ("Z", "X")


def history_items(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What one transcript record shows in the Open view, in order: the person's
    messages, Claude's text, tool names, and failed tool results. Tool inputs and
    outputs stay out, as they do for live events."""
    if rec.get("isSidechain") is True:
        return []
    kind = rec.get("type")
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    at = collector.iso_ms(rec.get("timestamp"))
    out: List[Dict[str, Any]] = []
    if kind == "user":
        if isinstance(content, str):
            if content.strip() and not content.lstrip().startswith("<"):
                out.append({"kind": "user", "text": collector.redact(content.strip())[:HISTORY_TEXT], "at": at})
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    out.append({"kind": "tool_result", "error": True, "at": at})
    elif kind == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                out.append({"kind": "text", "text": collector.redact(b["text"].strip())[:HISTORY_TEXT], "at": at})
            elif b.get("type") == "tool_use":
                out.append({"kind": "tool", "name": str(b.get("name"))[:80], "at": at})
    return out


def approval_summary(tool_name: str, tool_input: Any) -> str:
    """One line for the approval card: the command, the path, or the tool name."""
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "url", "pattern"):
            if isinstance(tool_input.get(key), str):
                return f"{tool_name}: {tool_input[key]}"
    return tool_name


class Broker:
    def __init__(self, cfg: Settings, verify: Callable[[str], bool]) -> None:
        cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cfg = cfg
        self.verify = verify
        self.lock = threading.RLock()  # Session() is built under it, and it calls ring_for(), which takes it too
        self.drivers: Dict[str, Session] = {}
        self.by_ticket: Dict[str, Session] = {}
        self.todo_lock = threading.Lock()   # one writer at a time for the todo files
        self.pr_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self.rings: "OrderedDict[str, Ring]" = OrderedDict()

    MAX_HISTORY = 50  # sessions whose recent events the broker keeps in memory

    def ring_for(self, sid: str) -> Ring:
        with self.lock:
            ring = self.rings.get(sid)
            if ring is None:
                ring = self.rings[sid] = Ring(self.cfg.ring)
                while len(self.rings) > self.MAX_HISTORY:
                    self.rings.popitem(last=False)
            return ring

    # Registry --------------------------------------------------------------

    def forget(self, s: Session) -> None:
        with self.lock:
            if self.drivers.get(s.sid) is s:
                del self.drivers[s.sid]
            self.by_ticket.pop(s.ticket, None)
        try:
            s.mcp_path.unlink()
        except FileNotFoundError:
            pass

    def _session(self, sid: str, model: str) -> Session:
        with self.lock:
            s = self.drivers.get(sid)
            if s is not None and s.proc.poll() is None:
                return s
        terminal = self.terminal_pid(sid)
        if terminal is not None:
            raise BrokerError("a terminal holds this session; take it over first", 409)
        cwd = self.check_cwd(self.session_cwd(sid))
        with self.lock:
            if len(self.drivers) >= self.cfg.max_drivers:
                raise BrokerError("too many sessions are open; stop one first", 429)
            s = Session(sid, cwd, model, self, resume=True)
            self.drivers[sid] = s
            self.by_ticket[s.ticket] = s
        return s

    @staticmethod
    def _model_of(_req: Dict[str, Any]) -> str:
        """Always the top model this subscription can use. The request's model field is
        ignored: the caller can't pick a smaller one. (Fable needs usage credits, so the
        top available is Opus; revisit when that changes.)"""
        return MODEL

    def check_cwd(self, cwd: Any) -> str:
        if not isinstance(cwd, str) or not cwd:
            raise BrokerError("cwd must be a path", 400)
        real = os.path.realpath(cwd)
        for root in self.cfg.roots:
            r = str(root)
            if real == r or real.startswith(r + os.sep):
                if not os.path.isdir(real):
                    raise BrokerError("cwd is not a directory", 400)
                return real
        raise BrokerError("cwd is outside the allowed projects", 403)

    def terminal_pid(self, sid: str) -> Optional[int]:
        """The pid of a live Claude Code process, other than ours, holding this session."""
        sessions = self.cfg.claude_home / "sessions"
        try:
            names = os.listdir(sessions)
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                d = json.loads((sessions / name).read_bytes())
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("sessionId") != sid:
                continue
            pid = d.get("pid")
            ps = d.get("procStart")
            if isinstance(pid, int) and pid != os.getpid() and collector.pid_alive(pid, str(ps) if ps is not None else None):
                return pid
        return None

    def transcript_path(self, sid: str) -> Optional[Path]:
        paths = list((self.cfg.claude_home / "projects").glob(f"*/{sid}.jsonl"))
        return max(paths, key=lambda p: p.stat().st_mtime) if paths else None

    def _history(self, _req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        """The conversation as the transcript on disk has it, so a terminal session shows
        its history too. `cursor` is the live event number at the moment it was read:
        events after it are new. A reply still being written can show up in both."""
        if sid is None:
            raise BrokerError("sessionId is needed", 400)
        with self.lock:
            ring = self.rings.get(sid)
            cursor = ring.seq if ring is not None else 0
        path = self.transcript_path(sid)
        if path is None:
            raise BrokerError("no transcript for this session", 404)
        size = path.stat().st_size
        start = max(0, size - HISTORY_BYTES)
        with path.open("rb") as f:
            f.seek(start)
            lines = f.read().split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line was cut at the window's start
        items: List[Dict[str, Any]] = []
        for line in lines:
            rec = collector._record(line)
            if rec is not None:
                items.extend(history_items(rec))
        return {"items": items[-HISTORY_ITEMS:], "cursor": cursor,
                "truncated": start > 0 or len(items) > HISTORY_ITEMS}

    def _session_file(self, sid: str) -> Optional[Dict[str, Any]]:
        sessions = self.cfg.claude_home / "sessions"
        try:
            names = os.listdir(sessions)
        except OSError:
            return None
        for name in names:
            if name.endswith(".json"):
                try:
                    d = json.loads((sessions / name).read_bytes())
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("sessionId") == sid:
                    return d
        return None

    def terminal_idle(self, sid: str) -> bool:
        """Idle by both signals: the session file says so, and its transcript has been
        quiet for TRANSCRIPT_QUIET_S. Anything that can't be checked counts as busy."""
        d = self._session_file(sid)
        if d is None or d.get("status") != "idle":
            return False
        paths = list((self.cfg.claude_home / "projects").glob(f"*/{sid}.jsonl"))
        if not paths:
            return False
        return time.time() - max(p.stat().st_mtime for p in paths) > TRANSCRIPT_QUIET_S

    def session_cwd(self, sid: str) -> str:
        """Where the session runs: from its session file, else from its transcript
        (every record carries cwd). The client never names a directory."""
        sessions = self.cfg.claude_home / "sessions"
        try:
            names = os.listdir(sessions)
        except OSError:
            names = []
        for name in names:
            if name.endswith(".json"):
                try:
                    d = json.loads((sessions / name).read_bytes())
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("sessionId") == sid and isinstance(d.get("cwd"), str):
                    return d["cwd"]
        for path in (self.cfg.claude_home / "projects").glob(f"*/{sid}.jsonl"):
            with path.open("rb") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                        return rec["cwd"]
        raise BrokerError("no such session on this machine", 404)

    def driver_of(self, sid: str) -> str:
        with self.lock:
            s = self.drivers.get(sid)
            if s is not None and s.proc.poll() is None:
                return "web"
        return "terminal" if self.terminal_pid(sid) is not None else "none"

    # Actions ---------------------------------------------------------------

    def dispatch(self, req: Dict[str, Any], channel: str = "web") -> Dict[str, Any]:
        """One request. Two sockets, two channels: the web socket (the only one the
        container mounts) takes the web actions; the approval socket, which only
        host-side approval.py uses, takes approval_request. Neither takes the other's."""
        action = req.get("action")
        if (action == "approval_request") != (channel == "approval"):
            self.audit(str(action), None, None, False, f"refused on the {channel} socket")
            return {"ok": False, "status": 404, "error": "not found"}
        if channel == "approval":
            return self._approval_request(req)
        token = req.get("token")
        if not isinstance(token, str) or not token or not self.verify(token):
            self.audit("refused", None, None, False, "no valid Access token")
            return {"ok": False, "status": 403, "error": "forbidden"}
        handler = {
            "start": self._start, "send": self._send, "events": self._events,
            "history": self._history,
            "todos": self._todos, "todo_set": self._todo_set, "prs": self._prs,
            "approve": self._approve, "stop": self._stop, "release": self._release,
            "take_over": self._take_over,
        }.get(action)
        if handler is None:
            return {"ok": False, "status": 400, "error": "unknown action"}
        sid = req.get("sessionId")
        if sid is not None and (not isinstance(sid, str) or not UUID.fullmatch(sid)):
            return {"ok": False, "status": 400, "error": "bad session id"}
        client = req.get("clientId") if isinstance(req.get("clientId"), str) else ""
        try:
            result = handler(req, sid)
        except BrokerError as e:
            self.audit(action, sid, client, False, str(e))
            return {"ok": False, "status": e.status, "error": str(e)}
        self.audit(action, sid, client, True, "")
        return dict(result, ok=True)

    def _start(self, req: Dict[str, Any], _sid: Optional[str]) -> Dict[str, Any]:
        text = self._text(req)
        model = self._model_of(req)
        cwd = self.check_cwd(req.get("cwd"))
        sid = str(uuid.uuid4())
        with self.lock:
            if len(self.drivers) >= self.cfg.max_drivers:
                raise BrokerError("too many sessions are open; stop one first", 429)
            s = Session(sid, cwd, model, self, resume=False)
            self.drivers[sid] = s
            self.by_ticket[s.ticket] = s
        s.send(text, self._client(req))
        return {"sessionId": sid}

    def _send(self, req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        if sid is None:
            raise BrokerError("sessionId is needed", 400)
        text = self._text(req)
        s = self._session(sid, self._model_of(req))
        return dict(s.send(text, self._client(req)), sessionId=sid)

    def _events(self, req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        if sid is None:
            raise BrokerError("sessionId is needed", 400)
        after = req.get("after", 0)
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise BrokerError("after must be a count", 400)
        with self.lock:
            s = self.drivers.get(sid)
            ring = self.rings.get(sid)
        events, gap = ring.since(after) if ring is not None else ([], False)
        # A cursor ahead of this broker's numbering means the broker restarted since the
        # client read its cursor; the client must reload the transcript.
        reset = after > (ring.seq if ring is not None else 0)
        return {"events": events, "gap": gap, "reset": reset, "driver": self.driver_of(sid),
                "busy": bool(s and s.busy), "approvals": s.pending() if s else []}

    def _approve(self, req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        if sid is None or not isinstance(req.get("approvalId"), str) or not isinstance(req.get("allow"), bool):
            raise BrokerError("sessionId, approvalId and allow (boolean) are needed", 400)
        with self.lock:
            s = self.drivers.get(sid)
        if s is None:
            raise BrokerError("the session isn't open", 404)
        s.decide(req["approvalId"], req["allow"])
        return {}

    def _stop(self, _req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        with self.lock:
            s = self.drivers.get(sid) if sid else None
        if s is None:
            raise BrokerError("the session isn't open", 404)
        s.stop(10)
        return {}

    def _release(self, _req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        with self.lock:
            s = self.drivers.get(sid) if sid else None
        if s is None:
            raise BrokerError("the session isn't open", 404)
        s.release()
        return {}

    def _take_over(self, _req: Dict[str, Any], sid: Optional[str]) -> Dict[str, Any]:
        """Stop a terminal's process so the web can resume the conversation. Only when
        the terminal is idle: its session file says so, and its transcript hasn't been
        written for TRANSCRIPT_QUIET_S. (The session file's status can lag mid-turn.)"""
        if sid is None:
            raise BrokerError("sessionId is needed", 400)
        pid = self.terminal_pid(sid)
        if pid is None:
            return {"released": False}
        if not self.terminal_idle(sid):
            raise BrokerError("the terminal is still answering; take over once it has finished", 409)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + self.cfg.terminal_stop_wait
        while time.monotonic() < deadline:
            if exited(pid):
                return {"released": True}
            time.sleep(0.2)
        raise BrokerError("the terminal's process didn't stop", 409)

    def _approval_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """From approval.py, for one tool call. Blocks until a decision or timeout."""
        ticket = req.get("ticket")
        with self.lock:
            s = self.by_ticket.get(ticket) if isinstance(ticket, str) else None
        if s is None:
            self.audit("approval_request", None, None, False, "unknown ticket")
            return {"ok": False, "status": 403, "error": "unknown ticket"}
        tool = str(req.get("tool_name") or "tool")[:80]
        allow = s.request_approval(tool, approval_summary(tool, req.get("input")))
        self.audit("approval", s.sid, None, True, "allow" if allow else "deny")
        return {"ok": True, "allow": allow}

    # Projects, todos and pull requests (DESIGN.md §12) -------------------------

    def _todo_files(self) -> Dict[str, Path]:
        if self.cfg.todos_dir is None:
            raise BrokerError("no todos directory is configured (todosDir)", 503)
        return {p.stem: p for p in TODOS.project_files(self.cfg.todos_dir)}

    def _todos(self, _req: Dict[str, Any], _sid: Optional[str]) -> Dict[str, Any]:
        projects = []
        for name, path in sorted(self._todo_files().items()):
            try:
                items = TODOS.read_project(path)
            except OSError as e:
                # Surface it: the page shows the error for this project, not an empty list.
                projects.append({"name": name, "items": [], "error": f"cannot read the todo file: {e.strerror}"})
                continue
            projects.append({"name": name, "error": None, "items": [
                {"id": i.id, "state": i.state, "text": i.text, "section": i.section} for i in items]})
        return {"projects": projects}

    def _todo_set(self, req: Dict[str, Any], _sid: Optional[str]) -> Dict[str, Any]:
        project, item_id = req.get("project"), req.get("todoId")
        expected, to = req.get("expected"), req.get("to")
        if not (isinstance(project, str) and isinstance(item_id, str)
                and isinstance(expected, int) and isinstance(to, int)
                and not isinstance(expected, bool) and not isinstance(to, bool)):
            raise BrokerError("project, todoId and the integer states expected and to are needed", 400)
        files = self._todo_files()
        if project not in files:
            raise BrokerError("no such project", 404)
        path = files[project]
        with self.todo_lock:
            try:
                text = path.read_text()
                updated = TODOS.set_state(text, item_id, expected, to)
            except TODOS.TodoError as e:
                raise BrokerError(str(e), 409)
            TODOS.write_atomic(path, updated)
        return {"project": project, "todoId": item_id, "state": to}

    def _prs(self, _req: Dict[str, Any], _sid: Optional[str]) -> Dict[str, Any]:
        """Each configured repo's pull requests, from gh as matt. Cached for PR_CACHE_S.
        A repo that fails shows its error next to its name."""
        now = time.time()
        repos = []
        for project, repo in sorted(self.cfg.repos.items()):
            cached = self.pr_cache.get(project)
            if cached and now - cached[0] < PR_CACHE_S:
                repos.append(cached[1])
                continue
            entry = self._fetch_prs(project, repo)
            self.pr_cache[project] = (now, entry)
            repos.append(entry)
        return {"repos": repos}

    def _fetch_prs(self, project: str, repo: str) -> Dict[str, Any]:
        cmd = ["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "40",
               "--json", "number,title,url,isDraft,state,headRefName,updatedAt,mergedAt"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"project": project, "repo": repo, "prs": [], "error": f"gh could not run: {e.__class__.__name__}"}
        if out.returncode != 0:
            first = (out.stderr.strip().splitlines() or ["gh failed"])[0]
            return {"project": project, "repo": repo, "prs": [], "error": first[:200]}
        try:
            rows = json.loads(out.stdout or "[]")
        except ValueError:
            return {"project": project, "repo": repo, "prs": [], "error": "gh returned something unreadable"}
        cutoff = time.time() - PR_MERGED_DAYS * 86400
        prs = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            merged = collector.iso_ms(r.get("mergedAt"))
            if r.get("state") == "MERGED" and merged is not None and merged / 1000 < cutoff:
                continue  # merged long ago: out of the board
            prs.append({"number": r.get("number"), "title": str(r.get("title", ""))[:200],
                        "url": r.get("url") if str(r.get("url", "")).startswith("https://github.com/") else None,
                        "state": r.get("state"), "draft": bool(r.get("isDraft")),
                        "branch": str(r.get("headRefName", ""))[:120], "updatedAt": collector.iso_ms(r.get("updatedAt"))})
        return {"project": project, "repo": repo, "prs": prs, "error": None}

    @staticmethod
    def _text(req: Dict[str, Any]) -> str:
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BrokerError("text is needed", 400)
        if len(text) > MAX_TEXT:
            raise BrokerError(f"text is over {MAX_TEXT} characters", 413)
        return text

    @staticmethod
    def _client(req: Dict[str, Any]) -> str:
        c = req.get("clientId")
        return c if isinstance(c, str) and c else uuid.uuid4().hex

    def audit(self, action: str, sid: Optional[str], client: Optional[str], ok: bool, reason: str) -> None:
        """One line per action: who it was for, not what was said."""
        line = "%s %s session=%s client=%s ok=%s %s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), action, sid or "-",
            (client or "-")[:16], "yes" if ok else "no", reason.replace("\n", " ")[:120])
        try:
            with open(self.cfg.state_dir / "audit.log", "a") as f:
                f.write(line)
        except OSError as e:
            log.error("audit log write FAILED: %s", e.strerror)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        broker: Broker = self.server.broker  # type: ignore[attr-defined]
        channel: str = self.server.channel  # type: ignore[attr-defined]
        try:
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST:
                resp: Dict[str, Any] = {"ok": False, "status": 413, "error": "request too large"}
            else:
                req = json.loads(raw)
                resp = broker.dispatch(req, channel) if isinstance(req, dict) else {"ok": False, "status": 400, "error": "not an object"}
        except ValueError:
            resp = {"ok": False, "status": 400, "error": "not JSON"}
        except Exception:  # surface it: one line to the journal, and an error to the caller
            log.exception("broker request FAILED")
            resp = {"ok": False, "status": 500, "error": "broker error (see its log)"}
        self.wfile.write((json.dumps(resp, separators=(",", ":")) + "\n").encode())


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _listen(path: Path, channel: str, broker: "Broker") -> _Server:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    old = os.umask(0o177)  # sockets are created owner-only
    try:
        srv = _Server(str(path), _Handler)
    finally:
        os.umask(old)
    srv.broker = broker  # type: ignore[attr-defined]
    srv.channel = channel  # type: ignore[attr-defined]
    return srv


def serve(broker: Broker) -> None:
    """The web socket and the approval socket, each on its own thread. Only the web
    socket is ever mounted into a container."""
    web = _listen(broker.cfg.socket_path, "web", broker)
    approvals = _listen(broker.cfg.approval_path, "approval", broker)
    threading.Thread(target=approvals.serve_forever, daemon=True, name="approvals").start()
    log.info("broker listening: web and approval sockets")
    web.serve_forever()


def load_settings(raw: Dict[str, Any]) -> Settings:
    home = Path.home()
    state = Path(os.path.expanduser(raw.get("stateDir", "~/jobs/broker")))
    roots = [Path(os.path.realpath(os.path.expanduser(r))) for r in raw.get("roots", ["~/apps"])]
    if not raw.get("aud") or not raw.get("team"):
        raise SystemExit("broker config needs aud and team (the Access app's audience and team domain)")
    todos_dir = raw.get("todosDir")
    repos = raw.get("repos", {})
    if not isinstance(repos, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in repos.items()):
        raise SystemExit("config repos must map project names to owner/repo strings")
    return Settings(
        socket_path=state / "broker.sock", approval_path=state / "approval.sock", state_dir=state,
        claude_bin=os.path.expanduser(raw.get("claudeBin", str(home / ".local/bin/claude"))),
        claude_home=Path(os.path.expanduser(raw.get("claudeDir", "~/.claude"))),
        roots=roots, aud=raw["aud"], team=raw["team"],
        todos_dir=Path(os.path.expanduser(todos_dir)) if todos_dir else None,
        repos=repos,
    )


def access_verifier(aud: str, team: str) -> Callable[[str], bool]:
    """The same check as jobs' auth.py: signature against Access's keys, audience, issuer, expiry."""
    import jwt
    client = jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600, timeout=10)

    def verify(token: str) -> bool:
        try:
            key = client.get_signing_key_from_jwt(token).key
            jwt.decode(token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{team}",
                       options={"require": ["exp", "iat", "aud", "iss"]})
        except jwt.PyJWKClientConnectionError as e:
            log.error("Access signing keys fetch FAILED: %s", e)
            return False
        except jwt.PyJWTError:
            return False
        return True
    return verify


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="jobs broker")
    ap.add_argument("--config", required=True, type=Path)
    args = ap.parse_args(argv)
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s broker %(message)s")
    raw = json.loads(args.config.read_text())
    cfg = load_settings(raw)
    serve(Broker(cfg, access_verifier(cfg.aud, cfg.team)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
