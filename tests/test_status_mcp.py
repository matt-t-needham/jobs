import json
from pathlib import Path

import pytest

import status_mcp
from status_mcp import CardError, Context, handle, validate

PARENT = 4242


@pytest.fixture
def ctx(tmp_path):
    sessions = tmp_path / "sessions"
    cards = tmp_path / "cards"
    sessions.mkdir()
    cards.mkdir()
    (sessions / f"{PARENT}.json").write_text(json.dumps({"pid": PARENT, "sessionId": "abc-123"}))
    return Context(sessions, cards, PARENT, clock=lambda: 1_000)


def call(ctx, args):
    return handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "report_status", "arguments": args}}, ctx)["result"]


@pytest.mark.parametrize("args", [
    {"blocked": {"on": "the moon"}},
    {"blocked": "waiting"},
    {"steps": [{"text": "no done flag"}]},
    {"steps": [{"text": "x", "done": True}] * (status_mcp.MAX_STEPS + 1)},
    {"goal": 7},
])
def test_bad_arguments_are_refused_and_nothing_is_written(ctx, args):
    result = call(ctx, args)
    assert result["isError"] is True
    assert list(ctx.cards_dir.iterdir()) == []


def test_card_lands_for_the_session_whose_pid_is_the_parent(ctx):
    result = call(ctx, {"goal": "g", "steps": [{"text": "s", "done": False}],
                        "blocked": {"on": "limit", "why": "resets 11:30pm"}})
    assert result == {"content": [{"type": "text", "text": "Status recorded."}], "isError": False}
    body = json.loads((ctx.cards_dir / "abc-123.json").read_bytes())
    assert body == {"goal": "g", "now": None, "steps": [{"text": "s", "done": False}],
                    "blocked": {"on": "limit", "why": "resets 11:30pm"},
                    "sessionId": "abc-123", "updatedAt": 1_000}


def test_no_session_file_for_the_parent_is_refused(tmp_path):
    ctx = Context(tmp_path, tmp_path, 99, clock=lambda: 1)
    result = call(ctx, {"goal": "g"})
    assert result["isError"] is True
    assert "no session file" in result["content"][0]["text"]


def test_session_file_for_another_pid_is_refused(tmp_path):
    (tmp_path / "99.json").write_text(json.dumps({"pid": 1, "sessionId": "x"}))
    with pytest.raises(CardError):
        status_mcp.session_for_parent(tmp_path, 99)


def test_protocol_answers_initialize_and_lists_one_tool(ctx):
    init = handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, ctx)
    assert init["result"]["serverInfo"]["name"] == "jobs-status"
    tools = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ctx)["result"]["tools"]
    assert [t["name"] for t in tools] == ["report_status"]


def test_notifications_get_no_reply_and_unknown_methods_get_an_error(ctx):
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, ctx) is None
    err = handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"}, ctx)
    assert err["error"]["code"] == -32601
