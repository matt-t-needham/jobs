import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import collector
import config
import web


def snap(**over):
    s = {
        "version": 1, "machine": {"id": "bix", "label": "bix"},
        "site": {"name": "bix.computer", "url": "https://bix.computer/"},
        "generatedAt": int(time.time() * 1000), "claudeVersions": ["2.1.327"],
        "projects": [{"id": "bix", "name": "bix"}],
        "tasks": [{k: None for k in collector.TASK_KEYS} | {"id": "s1", "status": "working", "extra": "x"}],
        "warnings": [], "unknownTop": 1,
    }
    s.update(over)
    return s


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_ONLY", True)
    monkeypatch.setattr(config, "ACCESS_AUD", "")


def client(store, host="127.0.0.1"):
    return TestClient(web.create_app(store), base_url=f"http://{host}")


def test_no_aud_refuses_everything_but_healthz(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_ONLY", False)
    monkeypatch.setattr(config, "ACCESS_AUD", "")
    store = web.Store()
    store.set(snap())
    c = client(store, "jobs.bix.computer")
    for path in ("/", "/api/state", "/api/events", "/static/app.js", "/static/app.css"):
        assert c.get(path).status_code == 403, path
    assert c.get("/healthz").status_code == 200


def test_missing_jwt_refused_when_aud_set(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_ONLY", False)
    monkeypatch.setattr(config, "ACCESS_AUD", "a" * 64)
    assert client(web.Store(), "jobs.bix.computer").get("/api/state").status_code == 403


def test_bad_jwt_refused(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_ONLY", False)
    monkeypatch.setattr(config, "ACCESS_AUD", "a" * 64)
    r = client(web.Store(), "jobs.bix.computer").get(
        "/api/state", headers={"cf-access-jwt-assertion": "not.a.jwt"})
    assert r.status_code == 403


def test_local_only_rejects_foreign_host(local):
    assert client(web.Store(), "evil.example").get("/api/state").status_code == 400


def test_state_filters_unknown_keys(local):
    store = web.Store()
    store.set(snap())
    body = client(store).get("/api/state").json()
    assert set(body) == set(web.SNAPSHOT_KEYS)
    assert tuple(body["tasks"][0]) == collector.TASK_KEYS
    assert body["warnings"] == []


def test_stale_snapshot_warns_and_fails_healthz(local, monkeypatch):
    monkeypatch.setattr(config, "STALE_SECONDS", 60)
    store = web.Store()
    store.set(snap(generatedAt=int((time.time() - 600) * 1000)))
    c = client(store)
    assert c.get("/api/state").json()["warnings"][0].startswith("The collector hasn't reported for 10 min")
    assert c.get("/healthz").status_code == 503


def test_no_snapshot_yet(local):
    c = client(web.Store())
    assert c.get("/api/state").json()["warnings"] == ["Waiting for the collector's first snapshot."]
    assert c.get("/healthz").status_code == 503


def test_security_headers_and_static(local):
    c = client(web.Store())
    r = c.get("/")
    assert r.status_code == 200
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/static/app.js").headers["content-type"].startswith("text/javascript")


def test_ui_never_uses_innerhtml():
    js = (web.STATIC / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js


def test_event_stream_sends_changes_and_keepalives(monkeypatch):
    monkeypatch.setattr(web, "KEEPALIVE_SECONDS", 0)
    bodies = iter(["a", "a", "b"])
    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return calls["n"] > 3

    async def nosleep(_):
        return None

    async def collect():
        return [f async for f in web.event_stream(lambda: next(bodies), disconnected, nosleep)]

    assert asyncio.run(collect()) == ["data: a\n\n", ": keepalive\n\n", "data: b\n\n"]


def test_follow_file_loads_and_survives_bad_file(tmp_path, caplog):
    path = tmp_path / "state.json"
    store = web.Store()
    stop = threading.Event()
    t = threading.Thread(target=web.follow_file, args=(store, path, 0.05, stop.is_set), daemon=True)
    t.start()
    try:
        time.sleep(0.15)
        assert store.get() is None
        assert any("snapshot read FAILED" in r.message for r in caplog.records)
        collector.write_snapshot(path, snap())
        _wait(lambda: store.get() is not None)
        good = store.get()
        path.write_text("{broken")
        time.sleep(0.15)
        assert store.get() is good  # keeps the last good snapshot
        collector.write_snapshot(path, snap(warnings=["w"]))
        _wait(lambda: store.get()["warnings"] == ["w"])
    finally:
        stop.set()
        t.join(2)


def _wait(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")
