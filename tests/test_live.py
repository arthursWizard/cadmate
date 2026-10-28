import importlib.util
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from cadmate import live

BRIDGE = Path(live.__file__).parent / "freecad_scripts" / "live_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("live_bridge", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # FreeCAD is only imported when the bridge starts
    return mod


@pytest.fixture
def sock(monkeypatch):
    d = Path(tempfile.mkdtemp(dir="/tmp"))  # macOS caps socket paths at 104 bytes; pytest's tmp_path is longer
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


def test_session_reports_changes_since_last_look(sock):
    fake_bridge(sock, [
        {"ok": True, "seq": 4, "events": [ev(4, "changed", "Box", "Length")]},
        {"ok": True, "seq": 6, "events": [ev(5, "changed", "Box", "Height"), ev(6, "changed", "Box", "Width", source="cadmate")]},
        {"ok": True, "seq": 1, "events": [ev(1, "changed", "Box", "Length")]},  # FreeCAD restarted
    ])
    s = live.Session()
    assert s.user_changes() == []  # the first look reads the state instead
    assert [c["properties"] for c in s.user_changes()] == [["Height"]]
    assert s.user_changes() == [] and s.seen == 1


def test_bridge_jsonable():
    b = load_bridge()

    class Q:
        Value, Unit = 2.5, "mm"

    class Vec:
        x, y, z = 1, 2, 3

    assert b._jsonable({"a": Q(), "b": [Vec()], "c": None}) == {"a": 2.5, "b": [[1, 2, 3]], "c": None}
