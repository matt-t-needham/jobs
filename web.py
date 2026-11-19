"""jobs server: serves the snapshot, pushes changes over SSE, and serves the UI.

It never reads ~/.claude. In the split shape (bix) it follows the snapshot file
the host collector writes; in the single-process shape it runs the collector in
a thread. See DESIGN.md §3–4.
"""
import asyncio
import json
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

import auth
import collector
import config

log = logging.getLogger("jobs")
STATIC = Path(__file__).parent / "static"
SNAPSHOT_KEYS = ("version", "machine", "site", "generatedAt", "claudeVersions", "projects", "tasks", "warnings")
KEEPALIVE_SECONDS = 15

CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' https://bix.computer",
    "img-src https://bix.computer",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])


def setup_logging() -> None:
    logging.Formatter.converter = time.gmtime
    fmt = logging.Formatter("%(asctime)s %(levelname)s jobs %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if config.LOG_DIR is not None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(config.LOG_DIR / "app.log", maxBytes=10_000_000, backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)


class Store:
    """The latest snapshot, shared between the source thread and requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: Optional[Dict[str, Any]] = None

    def set(self, snap: Dict[str, Any]) -> None:
        with self._lock:
            self._snap = snap

    def get(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._snap


def valid_snapshot(snap: Any) -> bool:
    return (isinstance(snap, dict) and isinstance(snap.get("tasks"), list)
            and isinstance(snap.get("generatedAt"), int) and snap.get("version") == collector.SNAPSHOT_VERSION)


def view(snap: Optional[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """What the API returns: the snapshot cut down to known keys (defence in
    depth: the collector already copies by name), plus a staleness warning."""
    if snap is None:
        return {"version": collector.SNAPSHOT_VERSION, "machine": None, "site": None, "generatedAt": None,
                "claudeVersions": [], "projects": [], "tasks": [],
                "warnings": ["Waiting for the collector's first snapshot."]}
    out = {k: snap.get(k) for k in SNAPSHOT_KEYS}
    out["tasks"] = [{k: t.get(k) for k in collector.TASK_KEYS}
                    for t in snap.get("tasks", []) if isinstance(t, dict)]
    warnings = [w for w in (snap.get("warnings") or []) if isinstance(w, str)]
    age = now - snap["generatedAt"] / 1000
    if age > config.STALE_SECONDS:
        warnings.insert(0, f"The collector hasn't reported for {int(age // 60)} min, so tiles may be out of date.")
    out["warnings"] = warnings
    return out


def follow_file(store: Store, path: Path, interval: float = 1.0,
                stop: Optional[Callable[[], bool]] = None) -> None:
    """Reload the snapshot whenever the collector replaces it."""
    last_key = None
    problem: Optional[str] = None
    while not (stop and stop()):
        try:
            st = path.stat()
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            if key != last_key:
                snap = json.loads(path.read_bytes())
                if not valid_snapshot(snap):
                    raise ValueError("not a jobs snapshot (version or fields)")
                store.set(snap)
                last_key = key
            if problem:
                log.info("snapshot readable again")
                problem = None
        except (OSError, ValueError) as e:
            msg = f"snapshot read FAILED: {e}"
            if msg != problem:
                log.error("%s", msg)
                problem = msg
        time.sleep(interval)


def run_collector(store: Store, cfg_path: Path) -> None:
    try:
        cfg = collector.load_config(cfg_path)
    except collector.ConfigError as e:
        log.critical("collector config FAILED: %s", e)
        return
    collector.run_forever(cfg, store.set)


async def event_stream(body: Callable[[], str], disconnected: Callable[[], Awaitable[bool]],
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> AsyncIterator[str]:
    """SSE frames: the full view whenever it changes, a comment as keepalive."""
    last, last_sent = None, time.monotonic()
    while not await disconnected():
        current = body()
        if current != last:
            yield f"data: {current}\n\n"
            last, last_sent = current, time.monotonic()
        elif time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            last_sent = time.monotonic()
        await sleep(1)


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="jobs", docs_url=None, redoc_url=None, openapi_url=None)
    if config.LOCAL_ONLY:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def current() -> Dict[str, Any]:
        return view(store.get(), time.time())

    @app.get("/healthz")
    def healthz():
        snap = store.get()
        if snap is None:
            return JSONResponse({"ok": False, "reason": "no snapshot yet"}, status_code=503)
        age = time.time() - snap["generatedAt"] / 1000
        if age > config.STALE_SECONDS:
            return JSONResponse({"ok": False, "reason": f"snapshot {int(age)} s old"}, status_code=503)
        return {"ok": True}

    gated = [Depends(auth.require_access)]

    @app.get("/", dependencies=gated)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/app.js", dependencies=gated)
    def app_js():
        return FileResponse(STATIC / "app.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/static/app.css", dependencies=gated)
    def app_css():
        return FileResponse(STATIC / "app.css", media_type="text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/api/state", dependencies=gated)
    def state():
        return JSONResponse(current(), headers={"Cache-Control": "no-store"})

    @app.get("/api/events", dependencies=gated)
    async def events(request: Request):
        def body() -> str:
            return json.dumps(current(), separators=(",", ":"))
        return StreamingResponse(
            event_stream(body, request.is_disconnected), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def main_app() -> FastAPI:
    setup_logging()
    store = Store()
    if config.SNAPSHOT:
        target, args, mode = follow_file, (store, Path(config.SNAPSHOT)), f"following {config.SNAPSHOT}"
    elif config.COLLECTOR_CONFIG:
        target, args, mode = run_collector, (store, Path(config.COLLECTOR_CONFIG)), "running the collector"
    else:
        raise SystemExit("set JOBS_SNAPSHOT (split) or JOBS_CONFIG (single process)")
    if config.LOCAL_ONLY:
        log.info("local-only: no Access check; serve on 127.0.0.1 only")
    elif not config.ACCESS_AUD:
        log.warning("ACCESS_AUD unset: every request is refused until it is set")
    threading.Thread(target=target, args=args, daemon=True, name="source").start()
    log.info("jobs server started, %s", mode)
    return create_app(store)
