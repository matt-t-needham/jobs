import json
import socket
import threading
from pathlib import Path

import pytest

from broker import approval


def serve_once(path: Path, reply: dict, seen: list):
    """A one-shot stand-in for the broker's approval socket: records the request and answers."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            seen.append(json.loads(conn.makefile("rb").readline()))
            conn.sendall((json.dumps(reply) + "\n").encode())
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def verdict(params_input, sock, ticket="t1"):
    params = {"name": "approve", "arguments": {"tool_name": "Bash", "input": params_input}}
    result = approval.decide(params, str(sock), ticket)
    return json.loads(result["content"][0]["text"])


def test_allowed_by_the_broker_means_allow_with_the_input_unchanged(tmp_path):
    sock = tmp_path / "a.sock"
    seen = []
    t = serve_once(sock, {"ok": True, "allow": True}, seen)
    v = verdict({"command": "ls"}, sock)
    t.join(5)
    assert v == {"behavior": "allow", "updatedInput": {"command": "ls"}}
    assert seen[0]["action"] == "approval_request" and seen[0]["ticket"] == "t1"
    assert seen[0]["tool_name"] == "Bash"


def test_denied_by_the_broker_means_deny(tmp_path):
    sock = tmp_path / "a.sock"
    t = serve_once(sock, {"ok": True, "allow": False}, [])
    assert verdict({}, sock)["behavior"] == "deny"
    t.join(5)


@pytest.mark.parametrize("reply", [{"ok": False, "status": 403}, {"ok": True}, "garbage"])
def test_anything_but_a_clear_allow_is_a_deny(tmp_path, reply):
    # An error, a reply without an answer, or a reply that isn't JSON must never allow.
    sock = tmp_path / "a.sock"
    body = reply if isinstance(reply, str) else json.dumps(reply)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            conn.makefile("rb").readline()
            conn.sendall((body + "\n").encode())
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert verdict({}, sock)["behavior"] == "deny"
    t.join(5)


def test_no_broker_at_all_is_a_deny(tmp_path):
    assert verdict({}, tmp_path / "missing.sock")["behavior"] == "deny"
