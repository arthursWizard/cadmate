import importlib.util
import json
import os
import shutil
import socket
import stat
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
    monkeypatch.delenv(live.TRANSPORT_ENV, raising=False)
    yield path
    shutil.rmtree(d, ignore_errors=True)


TRANSPORTS = [pytest.param("unix", marks=unix_sockets), "tcp"]


@pytest.fixture(params=TRANSPORTS)
def kind(request):
    return request.param


TOKEN = "fake-token"


def fake_bridge(path, replies, kind="unix"):
    """Answer each connection with the next reply, recording the requests (without the token, which it checks)."""
    if kind == "tcp":
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        live.endpoint_path().write_text(json.dumps({"host": "127.0.0.1", "port": srv.getsockname()[1], "token": TOKEN}),
                                        encoding="utf-8")
    else:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
    srv.listen(4)
    seen = []

    def serve():
        for reply in replies:
            conn, _ = srv.accept()
            with conn, conn.makefile("rwb") as f:
                req = json.loads(f.readline())
                assert req.pop("token", None) == (TOKEN if kind == "tcp" else None)
                seen.append(req)
                f.write((json.dumps(reply) + "\n").encode())
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return seen


def test_socket_path_matches_the_bridge(monkeypatch):
    monkeypatch.delenv(live.SOCKET_ENV, raising=False)
    assert load_bridge().socket_path() == live.socket_path()


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
@pytest.mark.parametrize("xdg", [None, "/run/user/1000"])
def test_paths_and_transport_match_the_bridge_on_each_platform(monkeypatch, system, xdg):
    bridge = load_bridge()
    for var in (live.SOCKET_ENV, live.TRANSPORT_ENV, "XDG_RUNTIME_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    if xdg:
        monkeypatch.setenv("XDG_RUNTIME_DIR", xdg)
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    monkeypatch.setattr(live.platform, "system", lambda: system)
    monkeypatch.setattr(bridge.platform, "system", lambda: system)
    for fn in ("data_dir", "socket_path", "endpoint_path", "transport"):
        assert getattr(bridge, fn)() == getattr(live, fn)(), fn
    assert live.transport() == ("tcp" if system == "Windows" else "unix")
    assert live.endpoint_path().parent == live.data_dir()
    monkeypatch.setenv(live.TRANSPORT_ENV, "TCP")
    assert live.transport() == bridge.transport() == "tcp"


def test_windows_data_folder_is_local_app_data(monkeypatch):
    monkeypatch.delenv(live.SOCKET_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(Path("/x/AppData/Local")))
    monkeypatch.setattr(live.platform, "system", lambda: "Windows")
    assert live.data_dir() == Path("/x/AppData/Local") / "cadmate"


def test_call_round_trip(sock, kind):
    seen = fake_bridge(sock, [{"ok": True, "bridge": 1}], kind)
    assert live.call("ping", timeout=3) == {"ok": True, "bridge": 1}
    assert seen == [{"op": "ping", "timeout": 3}]


def test_tcp_without_endpoint_file_is_not_running(sock, monkeypatch):
    monkeypatch.setenv(live.TRANSPORT_ENV, "tcp")
    with pytest.raises(live.BridgeUnavailable, match="isn't running"):
        live.call("ping")


def test_stale_endpoint_file_is_not_running(sock):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # a port nothing listens on, like a bridge whose FreeCAD crashed
    live.endpoint_path().write_text(json.dumps({"host": "127.0.0.1", "port": port, "token": "x"}), encoding="utf-8")
    with pytest.raises(live.BridgeUnavailable, match="isn't running"):
        live.call("ping", timeout=1)


def test_endpoint_off_this_machine_is_refused(sock):
    live.endpoint_path().write_text(json.dumps({"host": "10.0.0.5", "port": 1234, "token": "x"}), encoding="utf-8")
    with pytest.raises(live.BridgeUnavailable, match="not this machine"):
        live.call("ping")


def test_broken_endpoint_file_says_so(sock):
    live.endpoint_path().write_text("{not json", encoding="utf-8")
    with pytest.raises(live.BridgeUnavailable, match="endpoint file"):
        live.call("ping")


def test_bridge_not_running(sock):
    with pytest.raises(live.BridgeUnavailable):
        live.call("ping")
    r = live.not_running()
    assert not r["running"] and r["start"].startswith("exec(open(")


def test_start_command_points_at_a_copy_of_the_bridge(sock):
    cmd = live.start_command()
    assert cmd.startswith("exec(open(") and cmd.endswith(", encoding='utf-8').read())")
    copy = Path(eval(cmd[len("exec(open("):-len(", encoding='utf-8').read())")]))
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


def test_session_reports_changes_since_last_look(sock, kind):
    seen = fake_bridge(sock, [
        {"ok": True, "seq": 4, "events": [ev(4, "changed", "Box", "Length")]},
        {"ok": True, "seq": 6, "events": [ev(5, "changed", "Box", "Height"), ev(6, "changed", "Box", "Width", source="cadmate")]},
        {"ok": True, "seq": 1, "events": []},  # FreeCAD restarted: asks again from the start
        {"ok": True, "seq": 1, "events": [ev(1, "changed", "Box", "Radius")]},
    ], kind)
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


def test_old_bridge_is_reported_not_crashed(sock, kind):
    from cadmate import server

    fake_bridge(sock, [{"ok": True, "pong": True}], kind)
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


# The real bridge's socket side, without FreeCAD: Bridge.listen() and close() need only a Report view to print to.

class FakeApp:
    class Console:
        lines: list = []
        PrintMessage = PrintError = staticmethod(lambda line: FakeApp.Console.lines.append(line))


@pytest.fixture
def real_bridge(sock, kind):
    mod = load_bridge()
    bridge = mod.Bridge(FakeApp, kind)
    bridge._handle = lambda req: {"ok": True, "op": req["op"], "args": sorted(req)}
    bridge.listen()
    stop = threading.Event()

    def main_thread():  # stands in for FreeCAD's Qt timer
        while not stop.is_set():
            bridge._drain()
            stop.wait(0.01)

    threading.Thread(target=main_thread, daemon=True).start()
    yield bridge
    stop.set()
    bridge.close()


def test_real_bridge_round_trip(real_bridge, kind):
    r = live.call("state", timeout=5, doc="D")
    assert r["ok"] and r["op"] == "state"
    assert ("token" in r["args"]) == (kind == "tcp")  # cadmate sent the token; _handle strips it before the op runs
    assert live.call("ping_thread", timeout=5) == {"ok": True}


def test_tcp_bridge_refuses_a_wrong_or_missing_token(sock, monkeypatch):
    monkeypatch.setenv(live.TRANSPORT_ENV, "tcp")
    bridge = load_bridge().Bridge(FakeApp, "tcp")
    ran = []
    bridge._handle = lambda req: ran.append(req) or {"ok": True}
    bridge.listen()
    try:
        info = json.loads(live.endpoint_path().read_text(encoding="utf-8"))
        assert info["host"] == "127.0.0.1" and len(info["token"]) >= 32
        for token in (None, "guess", info["token"][:-1]):
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as s, s.makefile("rwb") as f:
                req = {"op": "run", "code": "print(1)"} | ({"token": token} if token else {})
                f.write((json.dumps(req) + "\n").encode())
                f.flush()
                reply = json.loads(f.readline())
            assert not reply["ok"] and "token" in reply["error"]
        bridge._drain()
        assert ran == [] and bridge.requests.empty()  # refused before it reached FreeCAD's queue
    finally:
        bridge.close()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="file modes; Windows relies on the profile folder's ACL")
def test_tcp_endpoint_file_is_private(real_bridge, kind):
    if kind != "tcp":
        pytest.skip("TCP only")
    assert stat.S_IMODE(os.stat(live.endpoint_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(live.data_dir()).st_mode) == 0o700


def test_tcp_bridge_cleans_up_only_its_own_endpoint_file(sock):
    mod = load_bridge()
    first = mod.Bridge(FakeApp, "tcp")
    first.listen()
    first.close()
    assert not live.endpoint_path().exists()
    a, b = mod.Bridge(FakeApp, "tcp"), mod.Bridge(FakeApp, "tcp")
    a.listen()
    b.listen()  # a second FreeCAD took over the endpoint file
    a.close()
    assert json.loads(live.endpoint_path().read_text(encoding="utf-8"))["token"] == b.token
    b.close()
    assert not live.endpoint_path().exists()


@unix_sockets
def test_unix_bridge_removes_a_stale_endpoint_file(sock):
    live.endpoint_path().parent.mkdir(parents=True, exist_ok=True)
    live.endpoint_path().write_text("{}", encoding="utf-8")  # left by a TCP bridge whose FreeCAD crashed
    bridge = load_bridge().Bridge(FakeApp, "unix")
    bridge.listen()
    try:
        assert not live.endpoint_path().exists() and sock.is_socket()
    finally:
        bridge.close()
