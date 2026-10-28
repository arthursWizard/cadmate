"""Talk to a running FreeCAD window through cadmate's bridge (freecad_scripts/live_bridge.py).

The interactive loop: cadmate opens or builds a model in the user's FreeCAD,
the user looks, comments or edits by hand, and cadmate edits on top of what is
there now. It never regenerates the document, so the user's edits stay. Before
each edit it reads what the user changed since its last look, from the
bridge's change log.

The bridge runs only when the user starts it from FreeCAD's Python console;
start_command() gives the line. It is copied to cadmate's data folder first so
that line keeps working when cadmate is reinstalled somewhere else.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
from pathlib import Path

SOCKET_ENV = "CADMATE_BRIDGE_SOCKET"
BRIDGE_VERSION = 1  # live_bridge.BRIDGE_VERSION this client speaks
BRIDGE_SOURCE = Path(__file__).parent / "freecad_scripts" / "live_bridge.py"


class BridgeUnavailable(RuntimeError):
    pass


def socket_path() -> Path:
    """Where the bridge listens. freecad_scripts/live_bridge.py has the same rule; keep them in step."""
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "cadmate" / "bridge.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(runtime) if runtime else Path.home() / ".cache") / "cadmate" / "bridge.sock"


def installed_bridge() -> Path:
    """The bridge script in cadmate's data folder, refreshed from the package when it changed."""
    target = socket_path().parent / "cadmate_bridge.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    if not target.exists() or target.read_bytes() != BRIDGE_SOURCE.read_bytes():
        shutil.copyfile(BRIDGE_SOURCE, target)
    return target


def start_command() -> str:
    """The line the user pastes into FreeCAD's Python console to start the bridge."""
    return f"exec(open({str(installed_bridge())!r}).read())"


def call(op: str, timeout: float = 120, **args) -> dict:
    """Send one request to the bridge and return its reply."""
    path = socket_path()
    if platform.system() == "Windows":
        raise BridgeUnavailable("the live FreeCAD bridge needs a Unix socket; it works on macOS and Linux only")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout + 5)
            s.connect(str(path))
            with s.makefile("rwb") as f:
                f.write((json.dumps({"op": op, "timeout": timeout} | args) + "\n").encode())
                f.flush()
                line = f.readline()
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise BridgeUnavailable(f"the FreeCAD bridge isn't running ({e.__class__.__name__} on {path})") from e
    except socket.timeout as e:
        raise BridgeUnavailable("the FreeCAD bridge didn't answer; FreeCAD may be busy or showing a dialog") from e
    except OSError as e:  # e.g. a socket path over the OS limit (104 bytes on macOS)
        raise BridgeUnavailable(f"can't reach the FreeCAD bridge at {path}: {e}") from e
    if not line:
        raise BridgeUnavailable("the FreeCAD bridge closed the connection without answering")
    return json.loads(line)


def not_running() -> dict:
    return {
        "ok": False,
        "running": False,
        "start": start_command(),
        "how": "Ask the user to open FreeCAD, then View > Panels > Python console, and paste the start line. "
               "The Report view then shows '[cadmate] bridge listening on ...'. It stops with App._cadmate_bridge.stop().",
    }


def summarize(events: list[dict], source: str = "user") -> list[dict]:
    """Collapse change-log events from one source into one entry per object, in first-seen order.

    {"doc", "object", "label", "created", "deleted", "properties": [...]}, plus
    {"doc", "undo": n, "redo": n} entries for undo and redo in the GUI.
    """
    out: dict[tuple, dict] = {}
    for e in events:
        if e.get("source") != source:
            continue
        if e["kind"] in ("undo", "redo"):
            entry = out.setdefault((e["doc"], None), {"doc": e["doc"], "undo": 0, "redo": 0})
            entry[e["kind"]] += 1
            continue
        entry = out.setdefault((e["doc"], e["object"]), {"doc": e["doc"], "object": e["object"], "label": e.get("label"),
                                                         "created": False, "deleted": False, "properties": []})
        if e.get("label"):
            entry["label"] = e["label"]
        if e["kind"] == "created":
            entry["created"] = True
        elif e["kind"] == "deleted":
            entry["deleted"] = True
        elif e.get("property") and e["property"] not in entry["properties"]:
            entry["properties"].append(e["property"])
    return list(out.values())


class Session:
    """Keeps the change-log position, so each call reports what the user did since cadmate last looked.

    The position is kept in a file next to the socket, so a new cadmate server
    (a new Claude session) carries on where the last one stopped instead of
    missing the edits made in between. With no position yet, every hand edit
    since the bridge started counts.
    """

    def __init__(self, path: Path | None = None):
        self.path = path

    def _file(self) -> Path:
        return self.path or socket_path().parent / "seen.json"

    def _load(self) -> int:
        try:
            return int(json.loads(self._file().read_text())["seq"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def user_changes(self) -> list[dict]:
        seen = self._load()
        r = call("changes", since=seen)
        if not r.get("ok") or "seq" not in r:
            raise BridgeUnavailable("the bridge running in FreeCAD is an older version; paste the start line again to replace it")
        if r["seq"] < seen:  # a restarted bridge counts from 0 again
            r = call("changes", since=0)
        try:
            self._file().write_text(json.dumps({"seq": r["seq"]}))
        except OSError:
            pass
        return summarize(r["events"])
