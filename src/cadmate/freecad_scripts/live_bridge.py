# cadmate's bridge into a running FreeCAD window. Runs inside the FreeCAD GUI,
# started by the user from FreeCAD's Python console (cadmate's freecad_live_status
# tool gives the exact line); it never starts on its own.
#
# On macOS and Linux it listens on a Unix socket in a folder only the user can
# open (0700, socket 0600), so a browser or another account can't reach it.
# On Windows (Python has no Unix sockets there), or with
# CADMATE_BRIDGE_TRANSPORT=tcp, it listens on a port on 127.0.0.1 and writes the
# port and a random token, new on each start, to bridge.json in the user's own
# folder (0600; on Windows %LOCALAPPDATA%, which only the user can read).
# Requests without that token are refused, so another account or a web page
# that finds the port gets nothing. Either way, any program running as the user
# can reach the bridge, as with any of their files.
#
# Protocol: one JSON object per line each way, one request per connection.
# Requests are queued and run on FreeCAD's main thread by a Qt timer, because
# FreeCAD's document and view must not be touched from another thread.
# Every request is printed in the Report view as "[cadmate] ...", and every
# edit is one undo step named "cadmate: <label>", so the user sees what cadmate
# did and can undo it with Ctrl+Z.
#
# A document observer logs every object change with a sequence number and
# whether it came from cadmate (during a request) or from the user, which is
# how cadmate learns what the user edited by hand between its own edits.
#
# Stop it with App._cadmate_bridge.stop() or the stop macro. Running this file again replaces
# the running bridge.
#
# On start it also saves two small macros in FreeCAD's macro folder:
# cadmate_bridge_start.FCMacro starts the bridge, so the user restarts it from
# Macro > Macros... (and then Macro > Recent macros) instead of pasting this
# line again, and cadmate_bridge_stop.FCMacro stops it. The start macro runs the
# copy of this file in cadmate's data folder, which cadmate refreshes on update.
import contextlib
import hmac
import io
import json
import os
import platform
import queue
import secrets
import socket
import stat
import tempfile
import threading
import time
import traceback
from pathlib import Path

BRIDGE_VERSION = 1
SOCKET_ENV = "CADMATE_BRIDGE_SOCKET"
TRANSPORT_ENV = "CADMATE_BRIDGE_TRANSPORT"
TICK_MS = 20
LOG_LIMIT = 5000
# Changed on every recompute or by FreeCAD itself; noise in a list of what someone edited.
QUIET_PROPERTIES = {"Shape", "Label2", "Visibility", "ExpressionEngine", "_Body"}
SKIP_STATE = {"Label", "Label2", "Visibility", "ExpressionEngine", "Placement", "Shape"}
STATE_TYPES = (
    "App::PropertyLength", "App::PropertyDistance", "App::PropertyAngle", "App::PropertyFloat",
    "App::PropertyFloatConstraint", "App::PropertyQuantity", "App::PropertyQuantityConstraint",
    "App::PropertyInteger", "App::PropertyIntegerConstraint", "App::PropertyPercent", "App::PropertyBool",
    "App::PropertyString", "App::PropertyEnumeration", "App::PropertyLink", "App::PropertyLinkList",
    "App::PropertyVector", "App::PropertyVectorDistance",
)


# Where things are. cadmate/live.py has the same rules; keep them in step (a test checks).

def data_dir() -> Path:
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override).expanduser().parent
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "cadmate"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "cadmate"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(runtime) if runtime else Path.home() / ".cache") / "cadmate"


def socket_path() -> Path:
    override = os.environ.get(SOCKET_ENV)
    return Path(override).expanduser() if override else data_dir() / "bridge.sock"


def endpoint_path() -> Path:
    return data_dir() / "bridge.json"


def transport() -> str:
    chosen = os.environ.get(TRANSPORT_ENV, "").strip().lower()
    if chosen in ("unix", "tcp"):
        return chosen
    return "tcp" if platform.system() == "Windows" or not hasattr(socket, "AF_UNIX") else "unix"


MACRO_NAME = "cadmate_bridge_start.FCMacro"  # starts the bridge
STOP_MACRO_NAME = "cadmate_bridge_stop.FCMacro"  # stops it
MACRO_MARK = "# cadmate: "  # the first line of every macro cadmate writes; others of the same name are left alone
_MACRO_NOTE = ("Written by cadmate's bridge, which rewrites it when it starts.\n"
               "# Delete it and it comes back the next time you paste cadmate's start line.\n")


def macro_texts(bridge: Path | None) -> dict:
    """The macros the bridge saves, by file name. The start macro needs the installed bridge copy to point at."""
    texts = {STOP_MACRO_NAME: (f"{MACRO_MARK}stops cadmate's FreeCAD bridge. {_MACRO_NOTE}"
                               "import FreeCAD\n"
                               "_bridge = getattr(FreeCAD, '_cadmate_bridge', None)\n"
                               "if _bridge is not None and _bridge.running:\n"
                               "    _bridge.stop()\n"
                               "else:\n"
                               "    FreeCAD.Console.PrintMessage(\"[cadmate] the bridge isn't running\\n\")\n")}
    if bridge is not None:
        texts[MACRO_NAME] = (f"{MACRO_MARK}starts cadmate's FreeCAD bridge. {_MACRO_NOTE}"
                             f"exec(open({str(bridge)!r}, encoding='utf-8').read())\n")
    return texts


def macro_dir(app) -> Path | None:
    """FreeCAD's macro folder as the user set it in Preferences > Python > Macro, or None."""
    for get in (lambda: app.getUserMacroDir(True),
                lambda: app.ParamGet("User parameter:BaseApp/Preferences/Macro").GetString("MacroPath"),
                lambda: str(Path(app.getUserAppDataDir()) / "Macro")):
        with contextlib.suppress(Exception):
            path = get()
            if path:
                return Path(path)
    return None


def install_macros(app, folder: Path | None = None, bridge: Path | None = None) -> dict:
    """Save the start and stop macros in FreeCAD's macro folder; returns {file name: path} of those in place.

    Leaves a same-named macro cadmate didn't write alone. The start macro is skipped when there is no installed
    bridge copy to point at (the bridge was run from somewhere else)."""
    bridge = bridge or data_dir() / "cadmate_bridge.py"
    folder = folder or macro_dir(app)
    if folder is None:
        return {}
    done = {}
    for name, text in macro_texts(bridge if bridge.is_file() else None).items():
        target = folder / name
        try:
            if target.exists():
                old = target.read_text(encoding="utf-8")
                if not old.startswith(MACRO_MARK):
                    continue
                if old != text:
                    target.write_text(text, encoding="utf-8")
            else:
                folder.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        done[name] = target
    return done


def _write_private(path: Path, text: str):
    """Write a file only the user can read (0600 where the OS has modes), replacing any old one in one step."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _jsonable(value):
    """FreeCAD values (quantities, vectors, placements, objects) as plain JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "Value") and hasattr(value, "Unit"):  # Quantity
        return round(value.Value, 6)
    if all(hasattr(value, a) for a in ("x", "y", "z")):  # Vector
        return [round(value.x, 6), round(value.y, 6), round(value.z, 6)]
    if hasattr(value, "Base") and hasattr(value, "Rotation"):  # Placement
        return _placement(value)
    if hasattr(value, "Name") and hasattr(value, "TypeId"):  # DocumentObject
        return value.Name
    return str(value)


VIEWS = ("isometric", "front", "rear", "top", "bottom", "left", "right")


def origin_objects(doc) -> list:
    """The origins in a document (one App::Origin per Part or Body) and their axes, planes and point.

    A coordinate system the user added on purpose is not an origin and is left out."""
    out = []
    for o in doc.Objects:
        is_origin = getattr(o, "TypeId", None) == "App::Origin"
        if not is_origin:
            with contextlib.suppress(Exception):
                is_origin = bool(o.isDerivedFrom("App::Origin"))
        if is_origin:
            for x in [o, *(getattr(o, "OriginFeatures", None) or [])]:
                if x not in out:
                    out.append(x)
    return out


def shown_origins(doc) -> list[str]:
    """Names of the origin objects in a document that are visible now."""
    return [o.Name for o in origin_objects(doc)
            if getattr(o, "ViewObject", None) is not None and o.ViewObject.Visibility]


def hide_origins(doc) -> list[str]:
    """Hide every origin object in a document; returns the names of those that were showing."""
    shown = shown_origins(doc)
    for o in origin_objects(doc):
        if o.Name in shown:
            o.ViewObject.Visibility = False
    return shown


# What a PartDesign body is built from rather than what it builds: sketches (the layout sketch too), datum planes,
# lines, points and coordinate systems, and binders. FreeCAD's own commands hide them once a feature uses them; code
# that adds features doesn't, so after a scripted round they cover the part (a datum plane is a sheet across it).
HELPER_TYPES = ("Sketcher::SketchObject", "PartDesign::Plane", "PartDesign::Line", "PartDesign::Point",
                "PartDesign::CoordinateSystem", "PartDesign::ShapeBinder", "PartDesign::SubShapeBinder")


def _is(o, types) -> bool:
    if getattr(o, "TypeId", None) in types:
        return True
    for t in types:
        with contextlib.suppress(Exception):
            if o.isDerivedFrom(t):
                return True
    return False


def _shown(o) -> bool:
    vo = getattr(o, "ViewObject", None)
    return vo is not None and bool(vo.Visibility)


def _show(o, on: bool) -> None:
    vo = getattr(o, "ViewObject", None)
    if vo is not None and bool(vo.Visibility) != on:
        vo.Visibility = on


def _has_solid(o) -> bool:
    shape = getattr(o, "Shape", None)
    return shape is not None and not shape.isNull() and bool(shape.Solids)


def _profile(feature):
    """The sketch a pad, pocket or other profile feature is made from, or None."""
    link = getattr(feature, "Profile", None)
    if isinstance(link, (tuple, list)):
        link = link[0] if link else None
    return link


def bodies(doc) -> list:
    return [o for o in doc.Objects if _is(o, ("PartDesign::Body",))]


def broken_tips(doc) -> list[dict]:
    """Bodies whose tip feature has no solid (it failed, or came out empty), with the last feature before it that has
    one. Such a body shows nothing but its sketches, though the part up to that feature is fine."""
    out = []
    for body in bodies(doc):
        tip = getattr(body, "Tip", None)
        if tip is None or _has_solid(tip):
            continue
        good, f, seen = None, getattr(tip, "BaseFeature", None), set()
        while f is not None and f.Name not in seen:
            seen.add(f.Name)
            if _has_solid(f):
                good = f
                break
            f = getattr(f, "BaseFeature", None)
        out.append({"body": body, "tip": tip, "good": good})
    return out


def hide_helpers(doc, keep=()) -> list[str]:
    """Hide the sketches, datums and binders inside each PartDesign body, except those in keep; returns the names of
    those that were showing. Sketches outside a body are the user's own and stay as they are."""
    hidden = []
    for body in bodies(doc):
        for o in getattr(body, "Group", None) or []:
            if o.Name not in keep and _is(o, HELPER_TYPES) and _shown(o):
                _show(o, False)
                hidden.append(o.Name)
    return hidden


def show_last_good(doc, broken=None) -> list[dict]:
    """For each body whose tip has no solid, show the last feature that has one, and the failing feature's sketch so
    the user sees where it goes. The tip stays where it is: this changes what is shown, not the model."""
    out = []
    for b in broken_tips(doc) if broken is None else broken:
        body, tip, good = b["body"], b["tip"], b["good"]
        if good is not None:
            for o in getattr(body, "Group", None) or []:
                if o is not good and not _is(o, HELPER_TYPES):
                    _show(o, False)
            _show(good, True)
        _show(body, True)
        sketch = _profile(tip)
        if sketch is not None:
            _show(sketch, True)
        out.append({"body": body.Name, "tip": tip.Name, "tip_label": tip.Label,
                    "showing": good.Name if good is not None else None,
                    "showing_label": good.Label if good is not None else None,
                    "sketch_shown": sketch.Name if sketch is not None else None})
    return out


def _placement(p) -> dict:
    yaw, pitch, roll = p.Rotation.getYawPitchRoll()
    return {"position": _jsonable(p.Base), "ypr": [round(yaw, 6), round(pitch, 6), round(roll, 6)]}


class Bridge:
    def __init__(self, app, kind: str = "unix", path: Path | None = None, endpoint: Path | None = None):
        self.App = app
        self.kind = kind
        self.path = path or socket_path()
        self.endpoint = endpoint or endpoint_path()
        self.token = None  # TCP only
        self.where = None
        self.requests: queue.Queue = queue.Queue()
        self.events: list[dict] = []
        self.seq = 0
        self.in_call = False
        self.running = True
        self.timer = None
        self.observer = _Observer(self)
        self.macros: dict = {}

    # Lifecycle

    def start(self):
        from PySide import QtCore

        self.listen()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._drain)
        self.timer.start(TICK_MS)
        self.App.addDocumentObserver(self.observer)
        self._say(f"bridge listening on {self.where}; stop it with App._cadmate_bridge.stop()")
        self.macros = install_macros(self.App)
        if MACRO_NAME in self.macros:
            self._say(f"next time, start it from Macro > Macros... > {MACRO_NAME} (then Macro > Recent macros), "
                      f"and stop it with {STOP_MACRO_NAME}; both are in {self.macros[MACRO_NAME].parent}")

    def stop(self):
        self.running = False
        if self.timer is not None:
            self.timer.stop()
        with contextlib.suppress(Exception):
            self.App.removeDocumentObserver(self.observer)
        self.close()
        self._say("bridge stopped")

    def listen(self):
        """Open the socket and accept connections on a background thread. Needs no FreeCAD, so tests run it."""
        d = self.endpoint.parent
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        if self.kind == "tcp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", 0))  # any free port; cadmate finds it in the endpoint file
            self.sock.listen(4)
            port = self.sock.getsockname()[1]
            self.token = secrets.token_urlsafe(32)
            _write_private(self.endpoint, json.dumps({"bridge": BRIDGE_VERSION, "host": "127.0.0.1", "port": port,
                                                      "token": self.token, "pid": os.getpid()}))
            self.where = f"127.0.0.1:{port} (port and token in {self.endpoint})"
        else:
            with contextlib.suppress(FileNotFoundError):
                self.endpoint.unlink()  # left by a TCP bridge that didn't stop cleanly; cadmate would follow it
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() or self.path.is_socket():
                self.path.unlink()
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(self.path))
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            self.sock.listen(4)
            self.where = str(self.path)
        threading.Thread(target=self._accept, name="cadmate-bridge", daemon=True).start()

    def close(self):
        self.running = False
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept thread
        with contextlib.suppress(OSError):
            self.sock.close()
        if self.kind == "tcp":
            with contextlib.suppress(OSError, ValueError, KeyError):
                if json.loads(self.endpoint.read_text(encoding="utf-8"))["token"] == self.token:
                    self.endpoint.unlink()  # only ours: another FreeCAD may have started a bridge since
        else:
            with contextlib.suppress(OSError):
                self.path.unlink()

    def _say(self, text: str, error: bool = False):
        line = f"[cadmate] {text}\n"
        (self.App.Console.PrintError if error else self.App.Console.PrintMessage)(line)

    # Socket side (background threads)

    def _accept(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn, conn.makefile("rwb") as f:
            line = f.readline()
            try:
                req = json.loads(line)
            except ValueError:
                reply = {"ok": False, "error": "request is not JSON"}
            else:
                if not isinstance(req, dict):
                    reply = {"ok": False, "error": "request is not a JSON object"}
                elif self.token is not None and not hmac.compare_digest(str(req.get("token", "")).encode(),
                                                                         self.token.encode()):
                    reply = {"ok": False, "error": "wrong or missing token; cadmate reads it from the bridge's endpoint file"}
                elif req.get("op") == "ping_thread":  # answers without the GUI, to tell a stuck GUI from a dead bridge
                    reply = {"ok": True}
                else:
                    reply = self._wait(_Request(req), float(req.get("timeout", 120)))
            f.write((json.dumps(reply) + "\n").encode())
            f.flush()

    def _wait(self, item, timeout: float) -> dict:
        """Queue a request for the main thread and wait for its reply.

        A request that hasn't started when the caller gives up is dropped, so
        it can't run later behind the caller's back (and run twice if the
        caller retries). One that has started can't be stopped; the reply says so.
        """
        self.requests.put(item)
        if item.done.wait(timeout):
            return item.reply
        with item.lock:
            if item.state == "queued":
                item.state = "dropped"
        if item.state == "dropped":
            return {"ok": False, "ran": False,
                    "error": "FreeCAD was busy (a dialog open, or a long operation) and didn't start this request in "
                             "time; it was dropped and nothing changed"}
        if item.state == "done":  # finished between the timeout and the lock
            return item.reply
        return {"ok": False, "ran": True,
                "error": "FreeCAD started this request but didn't finish it in time; it may still complete. "
                         "Look at the document (freecad_live_state) before trying again"}

    # GUI side (main thread)

    def _drain(self):
        while True:
            try:
                item = self.requests.get_nowait()
            except queue.Empty:
                return
            with item.lock:
                if item.state == "dropped":  # the caller gave up before it started
                    self._say(f"{item.req.get('op')} skipped: cadmate stopped waiting for it", error=True)
                    continue
                item.state = "running"
            try:
                item.reply = self._handle(item.req)
            except Exception as e:  # a reply always goes back, or the caller waits for the timeout
                self._say(f"{item.req.get('op')} failed: {e}", error=True)
                item.reply = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            item.state = "done"
            item.done.set()

    def _handle(self, req: dict) -> dict:
        op = req.get("op")
        fn = getattr(self, "op_" + str(op), None)
        if fn is None:
            return {"ok": False, "error": f"unknown op {op!r}"}
        summary = req.get("label") or req.get("path") or req.get("doc") or ""
        self._say(f"{op}{': ' + summary if summary else ''}")
        args = {k: v for k, v in req.items() if k not in ("op", "timeout", "token")}
        return {"ok": True} | fn(**args)

    def _doc(self, name: str | None):
        App = self.App
        doc = App.getDocument(name) if name else App.ActiveDocument
        if doc is None:
            raise ValueError("no document open" if not name else f"no document {name!r}")
        return doc

    def _log(self, kind: str, doc, obj=None, prop: str | None = None):
        if prop in QUIET_PROPERTIES or (prop or "").startswith("_"):
            return
        self.seq += 1
        self.events.append({
            "seq": self.seq, "time": round(time.time(), 3), "source": "cadmate" if self.in_call else "user",
            "kind": kind, "doc": getattr(doc, "Name", None), "object": getattr(obj, "Name", None),
            "label": getattr(obj, "Label", None), "property": prop,
        })
        del self.events[:-LOG_LIMIT]

    # Operations. Each returns a dict merged into {"ok": True}.

    def op_ping(self):
        App = self.App
        return {"bridge": BRIDGE_VERSION, "freecad": ".".join(App.Version()[:3]), "seq": self.seq,
                "documents": [d.Name for d in App.listDocuments().values()],
                "active": App.ActiveDocument.Name if App.ActiveDocument else None,
                "macros": {name: str(path) for name, path in self.macros.items()}}

    def op_state(self, doc=None):
        d = self._doc(doc)
        objects = [self._object(o) for o in d.Objects]
        gui = self._gui_doc(d)
        return {"doc": d.Name, "label": d.Label, "file": d.FileName or None,
                "modified": bool(getattr(gui, "Modified", False)), "undo": list(d.UndoNames),
                "seq": self.seq, "objects": objects}

    def _object(self, o) -> dict:
        out = {"name": o.Name, "label": o.Label, "type": o.TypeId, "parents": [p.Name for p in o.InList]}
        props = {}
        for p in o.PropertiesList:
            if p in SKIP_STATE or o.getTypeIdOfProperty(p) not in STATE_TYPES:
                continue
            with contextlib.suppress(Exception):
                if "Hidden" in o.getPropertyStatus(p):
                    continue
            with contextlib.suppress(Exception):
                props[p] = _jsonable(getattr(o, p))
        if props:
            out["properties"] = props
        if hasattr(o, "Placement"):
            out["placement"] = _placement(o.Placement)
        vo = getattr(o, "ViewObject", None)
        if vo is not None:
            out["visible"] = bool(vo.Visibility)
            with contextlib.suppress(Exception):
                out["colour"] = [round(c, 3) for c in vo.ShapeColor[:3]]
        shape = getattr(o, "Shape", None)
        if shape is not None and not shape.isNull():
            try:
                bb = shape.optimalBoundingBox()  # BoundBox is loose on curved faces
            except Exception:
                bb = shape.BoundBox
            out["bbox"] = [round(v, 4) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)]
            out["solids"] = len(shape.Solids)
            out["valid"] = shape.isValid()
        return out

    def _gui_doc(self, d):
        if not self.App.GuiUp:
            return None
        import FreeCADGui as Gui

        return Gui.getDocument(d.Name)

    def op_changes(self, since=0):
        return {"seq": self.seq, "events": [e for e in self.events if e["seq"] > since]}

    def op_run(self, code, label="edit", doc=None):
        import FreeCAD as App
        import FreeCADGui as Gui
        import Part

        d = self._doc(doc) if doc or App.ActiveDocument else None
        before = self.seq
        origins_before = {x.Name: set(shown_origins(x)) for x in App.listDocuments().values()}
        ns = {"App": App, "FreeCAD": App, "Gui": Gui, "FreeCADGui": Gui, "Part": Part, "V": App.Vector, "doc": d, "result": None}
        out = io.StringIO()
        if d is not None:
            d.openTransaction(f"cadmate: {label}")
        self.in_call = True
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(code, f"<cadmate: {label}>", "exec"), ns)
            for x in App.listDocuments().values():
                x.recompute()
        except Exception as e:
            if d is not None:
                d.abortTransaction()
            self._say(f"{label} failed and was undone: {e}", error=True)
            return {"ok": False, "error": str(e), "traceback": traceback.format_exc(), "stdout": out.getvalue()[-4000:]}
        finally:
            self.in_call = False
        if d is not None:
            d.commitTransaction()
        touched = sorted({e["object"] for e in self.events if e["seq"] > before and e["object"]})
        active = App.ActiveDocument
        reply = {"result": _jsonable(ns.get("result")), "stdout": out.getvalue()[-4000:], "touched": touched,
                 "doc": active.Name if active else None, "seq": self.seq}
        turned_on = {x.Name: sorted(set(shown_origins(x)) - origins_before.get(x.Name, set()))
                     for x in App.listDocuments().values()}
        turned_on = {k: v for k, v in turned_on.items() if v}
        if turned_on:  # the usual way origins end up on: code that shows every object in doc.Objects
            reply["origins_shown"] = turned_on
        return reply

    def op_read(self, code, label="read", doc=None):
        """Run code that only reads: no transaction, no recompute, so the user's undo list and modified flags stay as
        they were, even when an open document has a feature that won't recompute. The code sets `result`."""
        import FreeCAD as App
        import FreeCADGui as Gui
        import Part

        d = self._doc(doc) if doc or App.ActiveDocument else None
        name = d.Name if d is not None else None  # read now: the code may close the document
        ns = {"App": App, "FreeCAD": App, "Gui": Gui, "FreeCADGui": Gui, "Part": Part, "V": App.Vector, "doc": d, "result": None}
        out = io.StringIO()
        self.in_call = True
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(code, f"<cadmate: {label}>", "exec"), ns)
        except Exception as e:
            self._say(f"{label} failed: {e}", error=True)
            return {"ok": False, "error": str(e), "traceback": traceback.format_exc(), "stdout": out.getvalue()[-4000:]}
        finally:
            self.in_call = False
        return {"result": _jsonable(ns.get("result")), "stdout": out.getvalue()[-4000:], "doc": name, "seq": self.seq}

    def op_open(self, path, doc=None):
        import FreeCAD as App

        p = str(Path(path).expanduser())
        suffix = Path(p).suffix.lower()
        self.in_call = True
        try:
            if suffix == ".fcstd":
                d = App.openDocument(p)
            elif suffix in (".step", ".stp", ".iges", ".igs", ".brep"):
                import ImportGui

                d = self._doc(doc) if doc else App.newDocument(Path(p).stem.replace("-", "_").replace(".", "_"))
                ImportGui.insert(p, d.Name)
                d.recompute()
            elif suffix in (".py", ".fcmacro"):
                before = set(App.listDocuments())
                ns = {"__name__": "__main__", "__file__": p}
                exec(compile(Path(p).read_text(encoding="utf-8"), p, "exec"), ns)
                new = [n for n in App.listDocuments() if n not in before]
                d = App.getDocument(new[-1]) if new else App.ActiveDocument
            else:
                raise ValueError(f"can't open {suffix} files; give a .FCStd, STEP, IGES, BREP or a FreeCAD Python script")
        finally:
            self.in_call = False
        App.setActiveDocument(d.Name)
        # A saved .FCStd keeps the origins and sketches its owner left on; imports and build scripts are cadmate's to
        # tidy. A body whose tip failed shows its last good solid either way.
        tidy = self._tidy(d, view="isometric", fit=True, origins=suffix != ".fcstd", helpers=suffix != ".fcstd")
        return {"doc": d.Name, "seq": self.seq} | tidy

    def op_save(self, doc=None, path=None):
        d = self._doc(doc)
        if path:
            d.saveAs(str(Path(path).expanduser()))
        elif d.FileName:
            d.save()
        else:
            raise ValueError("the document has never been saved; give a path")
        gui = self._gui_doc(d)
        if gui is not None:
            with contextlib.suppress(Exception):
                gui.Modified = False  # FreeCAD 1.1 keeps the unsaved flag on the GUI document
        return {"doc": d.Name, "file": d.FileName}

    def op_export(self, path, doc=None, objects=None):
        d = self._doc(doc)
        if objects:
            objs = [d.getObject(n) for n in objects]
            missing = [n for n, o in zip(objects, objs) if o is None]
            if missing:
                raise ValueError(f"no objects {missing} in {d.Name}")
        else:  # what the user sees: visible solids that nothing else is built from
            objs = [o for o in d.Objects if getattr(o, "Shape", None) is not None and not o.Shape.isNull()
                    and o.Shape.Solids and not o.InList
                    and (getattr(o, "ViewObject", None) is None or o.ViewObject.Visibility)]
        if not objs:
            raise ValueError("nothing to export: no visible solids")
        p = str(Path(path).expanduser())
        if self.App.GuiUp:
            import ImportGui  # keeps the colours

            ImportGui.export(objs, p)
        else:
            import Import

            Import.export(objs, p)
        return {"file": p, "objects": [o.Name for o in objs]}

    def op_snapshot(self, doc=None, path=None, width=800, height=600, view="isometric", fit=True):
        import FreeCADGui as Gui

        d = self._doc(doc)
        self.App.setActiveDocument(d.Name)
        gd = Gui.getDocument(d.Name)
        Gui.ActiveDocument = gd
        v = gd.ActiveView
        camera = v.getCamera() if view or fit else None  # put the user's view back afterwards
        animate = getattr(v, "isAnimationEnabled", lambda: False)()
        try:
            if animate:
                v.setAnimationEnabled(False)  # or the picture can catch the camera mid-turn
            if view:
                getattr(v, "view" + view[:1].upper() + view[1:])()
            if fit:
                v.fitAll()
            Gui.updateGui()
            p = path or tempfile.mkstemp(prefix="cadmate-view-", suffix=".png")[1]
            v.saveImage(str(p), int(width), int(height), "Current")
        finally:
            if camera is not None:
                v.setCamera(camera)
            if animate:
                v.setAnimationEnabled(True)
        return {"file": str(p)}

    def op_tidy_view(self, doc=None, view="isometric", fit=True, hide_origins=True, hide_helpers=True):
        """Leave the document's view presentable: a standard view, the model fitted, origins and axis cross off,
        the sketches and datums inside bodies off, and the last good solid of a body whose tip failed."""
        return self._tidy(self._doc(doc), view=view, fit=fit, origins=hide_origins, helpers=hide_helpers)

    def _tidy(self, d, view="isometric", fit=True, origins=True, helpers=True) -> dict:
        if view and view not in VIEWS:
            raise ValueError(f"view {view!r} isn't one of {', '.join(VIEWS)}")
        out = {"doc": d.Name, "origins_hidden": hide_origins(d) if origins else []}
        broken = broken_tips(d)
        if helpers:  # the failing feature's sketch stays on: it's where the missing material should be
            out["helpers_hidden"] = hide_helpers(d, keep={s.Name for s in map(_profile, (b["tip"] for b in broken)) if s})
        if broken:
            out["no_solid"] = show_last_good(d, broken)
        if not self.App.GuiUp:
            return out
        import FreeCADGui as Gui

        self.App.setActiveDocument(d.Name)
        gd = Gui.getDocument(d.Name)
        Gui.ActiveDocument = gd
        v = gd.ActiveView
        with contextlib.suppress(Exception):  # bring this document's view to the front, so the user sees it
            Gui.getMainWindow().setActiveWindow(v)
        if origins:
            with contextlib.suppress(Exception):
                if v.hasAxisCross():
                    v.setAxisCross(False)
                    out["axis_cross_hidden"] = True
        animate = getattr(v, "isAnimationEnabled", lambda: False)()
        try:
            if animate:
                v.setAnimationEnabled(False)  # a turn still under way when fitAll runs fits the wrong side
            if view:
                getattr(v, "view" + view[:1].upper() + view[1:])()
            if fit:
                v.fitAll()  # after hiding the origins, or their planes count in the fit and the model ends up small
            Gui.updateGui()
        finally:
            if animate:
                v.setAnimationEnabled(True)
        return out | {"view": view or None, "fit": bool(fit)}


class _Request:
    """One request on its way to the main thread: queued, then running or dropped, then done."""

    def __init__(self, req: dict):
        self.req = req
        self.state = "queued"
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.reply: dict | None = None


class _Observer:
    def __init__(self, bridge: Bridge):
        self.b = bridge

    def slotCreatedObject(self, obj):
        self.b._log("created", obj.Document, obj)

    def slotDeletedObject(self, obj):
        self.b._log("deleted", obj.Document, obj)

    def slotChangedObject(self, obj, prop):
        self.b._log("changed", obj.Document, obj, prop)

    def slotUndoDocument(self, doc):
        self.b._log("undo", doc)

    def slotRedoDocument(self, doc):
        self.b._log("redo", doc)


def start() -> Bridge:
    import FreeCAD as App

    old = getattr(App, "_cadmate_bridge", None)
    if old is not None:
        old.stop()
    b = Bridge(App, transport())
    b.start()
    App._cadmate_bridge = b
    return b


if __name__ == "__main__":
    start()
