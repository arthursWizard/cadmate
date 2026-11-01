import importlib.util
import json
import shutil
import socket
import threading
from pathlib import Path

import pytest

from cadmate import live
from conftest import short_tmp, unix_sockets

BRIDGE = Path(live.__file__).parent / "freecad_scripts" / "live_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("live_bridge", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # FreeCAD is only imported when the bridge starts
    return mod


@pytest.fixture
def sock(monkeypatch):
    d = Path(short_tmp())
    path = d / "b.sock"
    monkeypatch.setenv(live.SOCKET_ENV, str(path))
    yield path
    shutil.rmtree(d, ignore_errors=True)


def fake_bridge(path, replies):
    """Answer each connection with the next reply, recording the requests."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    seen = []

    def serve():
        for reply in replies:
            conn, _ = srv.accept()
            with conn, conn.makefile("rwb") as f:
                seen.append(json.loads(f.readline()))
                f.write((json.dumps(reply) + "\n").encode())
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return seen


def test_socket_path_matches_the_bridge(monkeypatch):
    monkeypatch.delenv(live.SOCKET_ENV, raising=False)
    assert load_bridge().socket_path() == live.socket_path()


@unix_sockets
def test_call_round_trip(sock):
    seen = fake_bridge(sock, [{"ok": True, "bridge": 1}])
    assert live.call("ping", timeout=3) == {"ok": True, "bridge": 1}
    assert seen == [{"op": "ping", "timeout": 3}]


def test_bridge_not_running(sock):
    with pytest.raises(live.BridgeUnavailable):
        live.call("ping")
    r = live.not_running()
    assert not r["running"] and r["start"].startswith("exec(open(")


def test_start_command_points_at_a_copy_of_the_bridge(sock):
    cmd = live.start_command()
    copy = Path(eval(cmd[len("exec(open("):-len(").read())")]))
    assert copy.parent == sock.parent and copy.read_bytes() == BRIDGE.read_bytes()


def ev(seq, kind, obj=None, prop=None, source="user", doc="D"):
    return {"seq": seq, "source": source, "kind": kind, "doc": doc, "object": obj, "label": obj, "property": prop}


def test_summarize_keeps_user_edits_only():
    events = [
        ev(1, "changed", "Box", "Length", source="cadmate"),
        ev(2, "changed", "Box", "Width"),
        ev(3, "changed", "Box", "Width"),
        ev(4, "changed", "Box", "Placement"),
        ev(5, "created", "Fillet"),
        ev(6, "deleted", "Cylinder"),
        ev(7, "undo"),
    ]
    assert live.summarize(events) == [
        {"doc": "D", "object": "Box", "label": "Box", "created": False, "deleted": False, "properties": ["Width", "Placement"]},
        {"doc": "D", "object": "Fillet", "label": "Fillet", "created": True, "deleted": False, "properties": []},
        {"doc": "D", "object": "Cylinder", "label": "Cylinder", "created": False, "deleted": True, "properties": []},
        {"doc": "D", "undo": 1, "redo": 0},
    ]


@unix_sockets
def test_session_reports_changes_since_last_look(sock):
    seen = fake_bridge(sock, [
        {"ok": True, "seq": 4, "events": [ev(4, "changed", "Box", "Length")]},
        {"ok": True, "seq": 6, "events": [ev(5, "changed", "Box", "Height"), ev(6, "changed", "Box", "Width", source="cadmate")]},
        {"ok": True, "seq": 1, "events": []},  # FreeCAD restarted: asks again from the start
        {"ok": True, "seq": 1, "events": [ev(1, "changed", "Box", "Radius")]},
    ])
    assert [c["properties"] for c in live.Session().user_changes()] == [["Length"]]  # edits before the first look count
    assert [c["properties"] for c in live.Session().user_changes()] == [["Height"]]  # a new server carries on
    assert [c["properties"] for c in live.Session().user_changes()] == [["Radius"]]
    assert [r["since"] for r in seen] == [0, 4, 6, 0]


def test_bridge_jsonable():
    b = load_bridge()

    class Q:
        Value, Unit = 2.5, "mm"

    class Vec:
        x, y, z = 1, 2, 3

    assert b._jsonable({"a": Q(), "b": [Vec()], "c": None}) == {"a": 2.5, "b": [[1, 2, 3]], "c": None}


def test_bridge_versions_agree():
    assert load_bridge().BRIDGE_VERSION == live.BRIDGE_VERSION


@unix_sockets
def test_old_bridge_is_reported_not_crashed(sock):
    from cadmate import server

    fake_bridge(sock, [{"ok": True, "pong": True}])
    r = server.freecad_live_status()
    assert not r["running"] and "version None" in r["error"]


def bare_bridge():
    """A Bridge without FreeCAD: just the queue, enough to test how requests reach the main thread."""
    b = load_bridge()
    bridge = object.__new__(b.Bridge)
    bridge.requests = __import__("queue").Queue()
    bridge.handled = []
    bridge._handle = lambda req: bridge.handled.append(req["op"]) or {"ok": True, "op": req["op"]}
    bridge._say = lambda *a, **kw: None
    return b, bridge


def test_request_answered_by_the_main_thread():
    b, bridge = bare_bridge()
    item = b._Request({"op": "state"})
    t = threading.Thread(target=lambda: item.__setattr__("answer", bridge._wait(item, 5)))
    t.start()
    while bridge.requests.empty():
        pass
    bridge._drain()
    t.join()
    assert item.answer == {"ok": True, "op": "state"} and bridge.handled == ["state"]


def test_request_not_started_in_time_is_dropped_not_run_later():
    b, bridge = bare_bridge()
    item = b._Request({"op": "run"})
    r = bridge._wait(item, 0.05)  # the main thread is busy: nothing drains the queue
    assert not r["ok"] and r["ran"] is False and "nothing changed" in r["error"]
    bridge._drain()  # FreeCAD frees up later
    assert bridge.handled == []  # the edit the caller gave up on never runs


def test_request_started_but_slow_says_it_may_still_finish():
    b, bridge = bare_bridge()
    item = b._Request({"op": "run"})
    item.state = "running"  # the main thread picked it up and is still in it
    r = bridge._wait(item, 0.05)
    assert not r["ok"] and r["ran"] is True and "may still complete" in r["error"]
