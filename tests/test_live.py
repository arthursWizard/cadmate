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
    has_unix = hasattr(socket, "AF_UNIX")  # Python on Windows has none, whatever platform.system() says
    assert live.transport() == ("unix" if system != "Windows" and has_unix else "tcp")
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


# The start macro the bridge saves in FreeCAD's macro folder, so the user doesn't paste the start line again.

class MacroApp(FakeApp):
    folder = None

    @classmethod
    def getUserMacroDir(cls, actual=False):
        return str(cls.folder)


def test_bridge_saves_start_and_stop_macros(sock, tmp_path):
    mod = load_bridge()
    MacroApp.folder = tmp_path / "Macro"
    live.installed_bridge()
    saved = mod.install_macros(MacroApp)
    assert mod.MACRO_NAME == live.MACRO_NAME and mod.STOP_MACRO_NAME == live.STOP_MACRO_NAME
    assert saved == {name: MacroApp.folder / name for name in (live.MACRO_NAME, live.STOP_MACRO_NAME)}
    start = saved[live.MACRO_NAME].read_text(encoding="utf-8")
    assert start.startswith(mod.MACRO_MARK) and live.start_command() in start
    stop = saved[live.STOP_MACRO_NAME].read_text(encoding="utf-8")
    assert stop.startswith(mod.MACRO_MARK) and "_bridge.stop()" in stop
    for name, path in saved.items():
        compile(path.read_text(encoding="utf-8"), name, "exec")
    assert mod.install_macros(MacroApp) == saved  # the same again is fine


def test_stop_macro_stops_a_running_bridge_and_says_so_otherwise(sock, tmp_path):
    mod = load_bridge()
    stop = mod.macro_texts(None)[mod.STOP_MACRO_NAME]
    lines = []
    fake = type("FreeCAD", (), {"Console": type("C", (), {"PrintMessage": staticmethod(lines.append)})})
    __import__("sys").modules["FreeCAD"] = fake
    try:
        exec(stop, {})
        assert lines == ["[cadmate] the bridge isn't running\n"]
        bridge = type("B", (), {"running": True, "stop": lambda self: setattr(self, "running", False)})()
        fake._cadmate_bridge = bridge
        exec(stop, {})
        assert not bridge.running and len(lines) == 1
    finally:
        del __import__("sys").modules["FreeCAD"]


def test_bridge_leaves_a_macro_it_did_not_write_alone(sock, tmp_path):
    mod = load_bridge()
    live.installed_bridge()
    MacroApp.folder = tmp_path
    theirs = tmp_path / mod.MACRO_NAME
    theirs.write_text("print('mine')\n", encoding="utf-8")
    assert list(mod.install_macros(MacroApp)) == [mod.STOP_MACRO_NAME]
    assert theirs.read_text(encoding="utf-8") == "print('mine')\n"


def test_no_start_macro_without_an_installed_copy_and_none_without_a_macro_folder(sock, tmp_path):
    mod = load_bridge()
    MacroApp.folder = tmp_path
    assert list(mod.install_macros(MacroApp)) == [mod.STOP_MACRO_NAME]  # nothing in cadmate's data folder to start
    (tmp_path / mod.STOP_MACRO_NAME).unlink()
    live.installed_bridge()
    assert mod.install_macros(FakeApp) == {}  # FreeCAD didn't say where its macros go
    assert not list(tmp_path.iterdir())


def test_not_running_points_at_the_macros(sock):
    r = live.not_running()
    assert r["macro"] == live.MACRO_NAME and live.MACRO_NAME in r["how"] and live.STOP_MACRO_NAME in r["how"]


# Once per session, before FreeCAD work starts, the user hears whether the bridge is on and how to switch it.

@pytest.fixture
def fresh_session(monkeypatch):
    from cadmate import server
    monkeypatch.setattr(server, "_bridge_offered", False)
    return server


def test_status_tells_the_user_once_per_session(sock, fresh_session):
    first = fresh_session.freecad_live_status()
    assert not first["running"] and live.MACRO_NAME in first["tell_user"] and "once" in first["note"]
    again = fresh_session.freecad_live_status()
    assert not again["running"] and "tell_user" not in again


def test_offer_names_the_stop_macro_when_the_bridge_is_on():
    on, off = live.offer(True), live.offer(False)
    assert on["running"] and live.STOP_MACRO_NAME in on["tell_user"]
    assert not off["running"] and live.MACRO_NAME in off["tell_user"]


def test_first_freecad_tool_of_a_session_carries_the_check(sock, fresh_session):
    r = fresh_session.freecad_live_state()
    assert r["freecad_bridge"]["running"] is False and "tell_user" in r["freecad_bridge"]
    assert "freecad_bridge" not in fresh_session.freecad_live_state()
    assert "tell_user" not in fresh_session.freecad_live_status()  # already told


# Leaving the window presentable: origins off, a standard view, the model fitted.

class _VO:
    def __init__(self, visible):
        self.Visibility = visible


class _Obj:
    def __init__(self, name, type_id="Part::Feature", visible=True, features=()):
        self.Name, self.Label, self.TypeId, self.ViewObject = name, name, type_id, _VO(visible)
        if features:
            self.OriginFeatures = list(features)

    def isDerivedFrom(self, t):
        return self.TypeId == t


class _Doc:
    def __init__(self, name, objects):
        self.Name, self.Objects = name, objects


def _doc_with_origin(origin_on, axis_on, lcs_on=True):
    x_axis = _Obj("X_Axis", "App::Line", axis_on)
    xy = _Obj("XY_Plane", "App::Plane", axis_on)
    origin = _Obj("Origin", "App::Origin", origin_on, [x_axis, xy])
    lcs = _Obj("LCS", "App::Line", lcs_on)  # the user's own datum line, not part of an origin
    return _Doc("D", [_Obj("Body", "PartDesign::Body"), origin, x_axis, xy, _Obj("Box"), lcs])


def test_origin_objects_are_the_origins_and_their_axes_and_planes_only():
    b = load_bridge()
    d = _doc_with_origin(True, True)
    assert [o.Name for o in b.origin_objects(d)] == ["Origin", "X_Axis", "XY_Plane"]
    assert b.shown_origins(_doc_with_origin(False, True)) == ["X_Axis", "XY_Plane"]


def test_tidy_hides_the_origins_and_leaves_everything_else():
    b = load_bridge()
    d = _doc_with_origin(True, True)
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        r = bridge._tidy(d)
        assert r == {"doc": "D", "origins_hidden": ["Origin", "X_Axis", "XY_Plane"], "helpers_hidden": []}
        assert b.shown_origins(d) == []
        assert all(o.ViewObject.Visibility for o in d.Objects if o.Name in ("Body", "Box", "LCS"))
        assert bridge._tidy(d)["origins_hidden"] == []  # nothing left to hide
        d2 = _doc_with_origin(True, True)
        assert bridge._tidy(d2, origins=False)["origins_hidden"] == []  # the user asked for them on
        assert b.shown_origins(d2) == ["Origin", "X_Axis", "XY_Plane"]
        with pytest.raises(ValueError, match="sideways"):
            bridge._tidy(d2, view="sideways")
    finally:
        del FakeApp.GuiUp


class _Shape:
    def __init__(self, solids, null=None):
        self.Solids, self.null = [object()] * solids, (not solids) if null is None else null

    def isNull(self):
        return self.null


def _feature(name, base=None, solids=1, profile=None, type_id="PartDesign::Pad"):
    f = _Obj(name, type_id)
    f.Label, f.BaseFeature, f.Shape = name, base, _Shape(solids)
    if profile is not None:
        f.Profile = (profile, [""])
    f.OutList = [x for x in (profile, base) if x is not None]
    return f


def _body_doc(boss_solids=1):
    """A case built the freecad-modelling way: Params, a layout sketch, a datum plane, a shell and a boss on top."""
    layout, outline = _Obj("layout", "Sketcher::SketchObject"), _Obj("outline", "Sketcher::SketchObject")
    lid = _Obj("lid_plane", "PartDesign::Plane")
    boss_sketch = _Obj("boss_sketch", "Sketcher::SketchObject")
    for sk in (layout, outline, boss_sketch):
        sk.Shape = _Shape(0, null=False)  # a sketch that solved: wires, no solid
    shell = _feature("shell", profile=outline)
    boss = _feature("boss", base=shell, solids=boss_solids, profile=boss_sketch)
    shell.ViewObject.Visibility = False  # FreeCAD shows only the newest feature of a body
    body = _Obj("base", "PartDesign::Body")
    body.Group, body.Tip, body.Label = [layout, lid, outline, shell, boss_sketch, boss], boss, "base"
    loose = _Obj("notes", "Sketcher::SketchObject")  # a sketch outside any body is the user's own
    return _Doc("C", [_Obj("Params", "Spreadsheet::Sheet"), body, *body.Group, loose])


def _visible(d):
    return [o.Name for o in d.Objects if o.ViewObject.Visibility]


def test_tidy_hides_sketches_and_datums_inside_bodies():
    b = load_bridge()
    d = _body_doc()
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        r = bridge._tidy(d)
        assert r["helpers_hidden"] == ["layout", "lid_plane", "outline", "boss_sketch"] and "no_solid" not in r
        assert _visible(d) == ["Params", "base", "boss", "notes"]
        d2 = _body_doc()
        assert "helpers_hidden" not in bridge._tidy(d2, helpers=False)
        assert "layout" in _visible(d2)  # the user asked for sketches on
    finally:
        del FakeApp.GuiUp


def test_tidy_shows_the_last_good_solid_when_the_tip_failed():
    b = load_bridge()
    d = _body_doc(boss_solids=0)  # a pad on an over-constrained sketch: an empty shape
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        r = bridge._tidy(d)
        assert r["no_solid"] == [{"body": "base", "tip": "boss", "tip_label": "boss", "failed": "boss",
                                  "failed_label": "boss", "cause": "boss", "cause_label": "boss", "showing": "shell",
                                  "showing_label": "shell", "sketch_shown": "boss_sketch"}]
        assert _visible(d) == ["Params", "base", "shell", "boss_sketch", "notes"]
        body = d.Objects[1]
        assert body.Tip.Name == "boss"  # what's shown changes, not the model
        body.Group[3].Shape = _Shape(0)  # the shell fails too: it is the one to fix, and nothing builds
        r = bridge._tidy(d)
        e = r["no_solid"][0]
        assert (e["failed"], e["showing"], e["sketch_shown"]) == ("shell", None, "outline") and "sketch_empty" not in e
        assert _visible(d) == ["Params", "base", "outline", "notes"]
        body.Group[2].Shape = _Shape(0, null=True)  # and its sketch didn't solve either: say the view is empty
        assert bridge._tidy(d)["no_solid"][0]["sketch_empty"] is True
    finally:
        del FakeApp.GuiUp


def test_a_tip_that_failed_after_working_counts_as_failed_though_it_keeps_its_old_solid():
    b = load_bridge()
    d = _body_doc()
    boss = d.Objects[1].Tip
    boss.State = ["Invalid", "Touched"]  # FreeCAD keeps the last good shape when a recompute fails
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        assert bridge._tidy(d)["no_solid"][0]["showing"] == "shell"
        assert "boss" not in _visible(d)
    finally:
        del FakeApp.GuiUp


def test_a_broken_sketch_is_found_though_its_feature_still_reads_as_fine():
    b = load_bridge()
    d = _body_doc()
    body = d.Objects[1]
    outline = body.Group[2]
    outline.State = ["Touched", "Invalid"]  # the pad on it stays Up-to-date and keeps its old solid
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        e = bridge._tidy(d)["no_solid"][0]
        assert (e["failed"], e["cause"], e["showing"], e["sketch_shown"]) == ("shell", "outline", None, "outline")
        assert _visible(d) == ["Params", "base", "outline", "notes"]  # no stale solid on screen
        outline.State = []  # fixed: the next tidy shows the tip again, not the last view
        r = bridge._tidy(d)
        assert "no_solid" not in r and _visible(d) == ["Params", "base", "boss", "notes"]
    finally:
        del FakeApp.GuiUp


def test_a_failed_fillet_keeps_the_part_on_screen_and_says_it_is_stale():
    b = load_bridge()
    d = _body_doc()
    body = d.Objects[1]
    fillet = _feature("fillet", base=body.Tip, type_id="PartDesign::Fillet")
    fillet.State = ["Touched", "Invalid"]  # FreeCAD keeps its old shape, and nothing to outline
    body.Group.append(fillet)
    body.Tip = fillet
    d.Objects.insert(-1, fillet)
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        e = bridge._tidy(d)["no_solid"][0]
        assert (e["failed"], e["showing"], e["sketch_shown"], e["stale"]) == ("fillet", "fillet", None, True)
        assert "fillet" in _visible(d) and "boss" not in _visible(d)
    finally:
        del FakeApp.GuiUp


def test_tidy_leaves_a_saved_document_without_unsaved_changes():
    b = load_bridge()
    bridge = b.Bridge(FakeApp, "unix")
    gui = type("GuiDoc", (), {"Modified": False})()
    bridge._gui_doc = lambda d: gui
    inner = bridge._tidy_view

    def showing_marks_it_modified(*a, **kw):
        gui.Modified = True  # what FreeCAD does when a visibility changes
        return inner(*a, **kw)

    bridge._tidy_view = showing_marks_it_modified
    FakeApp.GuiUp = False
    try:
        bridge._tidy(_body_doc(boss_solids=0))
        assert gui.Modified is False
        gui.Modified = True  # real unsaved changes stay flagged
        bridge._tidy(_body_doc(boss_solids=0))
        assert gui.Modified is True
    finally:
        del FakeApp.GuiUp


def test_a_hidden_body_and_a_face_profile_are_left_alone():
    b = load_bridge()
    d = _body_doc(boss_solids=0)
    body, boss = d.Objects[1], d.Objects[1].Tip
    bridge = b.Bridge(FakeApp, "unix")
    FakeApp.GuiUp = False
    try:
        boss.Profile = (body.Group[3], ["Face6"])  # sketchless: a pad from the shell's top face
        r = bridge._tidy(d)
        assert r["no_solid"][0]["sketch_shown"] is None and "boss_sketch" not in _visible(d)
        d2 = _body_doc(boss_solids=0)
        d2.Objects[1].ViewObject.Visibility = False  # the user hid this body
        assert "no_solid" not in bridge._tidy(d2) and "shell" not in _visible(d2)
    finally:
        del FakeApp.GuiUp


def test_tool_against_an_older_bridge_says_to_restart_it(sock, kind, fresh_session):
    fresh_session._bridge_offered = True
    fake_bridge(sock, [{"ok": True, "seq": 0, "events": []}, {"ok": False, "error": "unknown op 'tidy_view'"}], kind)
    r = fresh_session.freecad_live_tidy_view()
    assert not r["ok"] and live.MACRO_NAME in r["how"]
    assert (live.data_dir() / "cadmate_bridge.py").read_bytes() == live.BRIDGE_SOURCE.read_bytes()  # the macro runs the new one
