# cadmate's bridge into a running FreeCAD window. Runs inside the FreeCAD GUI,
# started by the user from FreeCAD's Python console (cadmate's freecad_live_status
# tool gives the exact line); it never starts on its own.
#
# It listens on a Unix socket in a folder only the user can open (0700, socket
# 0600), so a browser or another account can't reach it. Any program running as
# the user can, as with any of their files.
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
# Stop it with App._cadmate_bridge.stop(). Running this file again replaces
# the running bridge.
import contextlib
import io
import json
import os
import platform
import queue
import socket
import stat
import tempfile
import threading
import time
import traceback
from pathlib import Path

BRIDGE_VERSION = 1
SOCKET_ENV = "CADMATE_BRIDGE_SOCKET"
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


def socket_path() -> Path:
    """Where the bridge listens. cadmate/live.py has the same rule; keep them in step."""
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "cadmate" / "bridge.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(runtime) if runtime else Path.home() / ".cache") / "cadmate" / "bridge.sock"


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


def _placement(p) -> dict:
    yaw, pitch, roll = p.Rotation.getYawPitchRoll()
    return {"position": _jsonable(p.Base), "ypr": [round(yaw, 6), round(pitch, 6), round(roll, 6)]}


class Bridge:
    def __init__(self, path: Path):
        import FreeCAD as App

        self.App = App
        self.path = path
        self.requests: queue.Queue = queue.Queue()
        self.events: list[dict] = []
        self.seq = 0
        self.in_call = False
        self.running = True
        self.timer = None
        self.observer = _Observer(self)

    # Lifecycle

    def start(self):
        from PySide import QtCore

        d = self.path.parent
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        if self.path.exists() or self.path.is_socket():
            self.path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self.sock.listen(4)
        threading.Thread(target=self._accept, name="cadmate-bridge", daemon=True).start()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._drain)
        self.timer.start(TICK_MS)
        self.App.addDocumentObserver(self.observer)
        self._say(f"bridge listening on {self.path}; stop it with App._cadmate_bridge.stop()")

    def stop(self):
        self.running = False
        if self.timer is not None:
            self.timer.stop()
        with contextlib.suppress(Exception):
            self.App.removeDocumentObserver(self.observer)
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept thread
        with contextlib.suppress(OSError):
            self.sock.close()
        with contextlib.suppress(OSError):
            self.path.unlink()
        self._say("bridge stopped")

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
                if req.get("op") == "ping_thread":  # answers without the GUI, to tell a stuck GUI from a dead bridge
                    reply = {"ok": True}
                else:
                    done = threading.Event()
                    box = {}
                    self.requests.put((req, box, done))
                    if not done.wait(float(req.get("timeout", 120))):
                        reply = {"ok": False, "error": "FreeCAD did not answer in time; it may be showing a dialog or busy"}
                    else:
                        reply = box["reply"]
            f.write((json.dumps(reply) + "\n").encode())
            f.flush()

    # GUI side (main thread)

    def _drain(self):
        while True:
            try:
                req, box, done = self.requests.get_nowait()
            except queue.Empty:
                return
            try:
                box["reply"] = self._handle(req)
            except Exception as e:  # a reply always goes back, or the caller waits for the timeout
                self._say(f"{req.get('op')} failed: {e}", error=True)
                box["reply"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            done.set()

    def _handle(self, req: dict) -> dict:
        op = req.get("op")
        fn = getattr(self, "op_" + str(op), None)
        if fn is None:
            return {"ok": False, "error": f"unknown op {op!r}"}
        summary = req.get("label") or req.get("path") or req.get("doc") or ""
        self._say(f"{op}{': ' + summary if summary else ''}")
        args = {k: v for k, v in req.items() if k not in ("op", "timeout")}
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
                "active": App.ActiveDocument.Name if App.ActiveDocument else None}

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
        return {"result": _jsonable(ns.get("result")), "stdout": out.getvalue()[-4000:], "touched": touched,
                "doc": active.Name if active else None, "seq": self.seq}

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
                exec(compile(Path(p).read_text(), p, "exec"), ns)
                new = [n for n in App.listDocuments() if n not in before]
                d = App.getDocument(new[-1]) if new else App.ActiveDocument
            else:
                raise ValueError(f"can't open {suffix} files; give a .FCStd, STEP, IGES, BREP or a FreeCAD Python script")
        finally:
            self.in_call = False
        App.setActiveDocument(d.Name)
        self._fit()
        return {"doc": d.Name, "seq": self.seq}

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
        if view:
            getattr(v, "view" + view[:1].upper() + view[1:])()
        if fit:
            v.fitAll()
        p = path or tempfile.mkstemp(prefix="cadmate-view-", suffix=".png")[1]
        v.saveImage(str(p), int(width), int(height), "Current")
        return {"file": str(p)}

    def _fit(self):
        if self.App.GuiUp:
            import FreeCADGui as Gui

            with contextlib.suppress(Exception):
                Gui.SendMsgToActiveView("ViewFit")


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
    b = Bridge(socket_path())
    b.start()
    App._cadmate_bridge = b
    return b


if __name__ == "__main__":
    start()
