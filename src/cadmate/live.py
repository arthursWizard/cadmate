"""Talk to a running FreeCAD window through cadmate's bridge (freecad_scripts/live_bridge.py).

The interactive loop: cadmate opens or builds a model in the user's FreeCAD,
the user looks, comments or edits by hand, and cadmate edits on top of what is
there now. It never regenerates the document, so the user's edits stay. Before
each edit it reads what the user changed since its last look, from the
bridge's change log.

The bridge runs only when the user starts it from FreeCAD's Python console;
start_command() gives the line. The first start saves a macro in FreeCAD's
macro folder that runs the same line, so after that the user starts it from
FreeCAD's Macro menu. The bridge is copied to cadmate's data folder first so
that line keeps working when cadmate is reinstalled somewhere else.

Two transports, chosen by the bridge when it starts. On macOS and Linux it
listens on a Unix socket in the data folder (0700). On Windows, which has no
Unix sockets in Python, or with CADMATE_BRIDGE_TRANSPORT=tcp, it listens on a
localhost TCP port and writes the port and a fresh random token to
bridge.json in the data folder, readable only by the user; every request
carries that token. The client follows bridge.json when there is one.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
from pathlib import Path

SOCKET_ENV = "CADMATE_BRIDGE_SOCKET"  # moves the socket, and the data folder with it
TRANSPORT_ENV = "CADMATE_BRIDGE_TRANSPORT"  # "unix" or "tcp"; the platform's default otherwise
BRIDGE_VERSION = 1  # live_bridge.BRIDGE_VERSION this client speaks
BRIDGE_SOURCE = Path(__file__).parent / "freecad_scripts" / "live_bridge.py"
LOOPBACK = ("127.0.0.1", "::1")
MACRO_NAME = "cadmate_bridge.FCMacro"  # live_bridge.MACRO_NAME, the macro the bridge saves in FreeCAD's macro folder


class BridgeUnavailable(RuntimeError):
    pass


# Where things are. freecad_scripts/live_bridge.py has the same rules; keep them in step (a test checks).

def data_dir() -> Path:
    """cadmate's per-user folder: the bridge's socket or endpoint file, its installed copy, and the live tools' state."""
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
    """The Unix socket the bridge listens on, when it uses one."""
    override = os.environ.get(SOCKET_ENV)
    return Path(override).expanduser() if override else data_dir() / "bridge.sock"


def endpoint_path() -> Path:
    """The file a TCP bridge writes its port and token to."""
    return data_dir() / "bridge.json"


def transport() -> str:
    """The transport a bridge started here uses: tcp on Windows, unix elsewhere, unless CADMATE_BRIDGE_TRANSPORT says."""
    chosen = os.environ.get(TRANSPORT_ENV, "").strip().lower()
    if chosen in ("unix", "tcp"):
        return chosen
    return "tcp" if platform.system() == "Windows" or not hasattr(socket, "AF_UNIX") else "unix"


def installed_bridge() -> Path:
    """The bridge script in cadmate's data folder, refreshed from the package when it changed."""
    target = data_dir() / "cadmate_bridge.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    if not target.exists() or target.read_bytes() != BRIDGE_SOURCE.read_bytes():
        shutil.copyfile(BRIDGE_SOURCE, target)
    return target


def start_command() -> str:
    """The line the user pastes into FreeCAD's Python console to start the bridge."""
    return f"exec(open({str(installed_bridge())!r}, encoding='utf-8').read())"


def _connect(timeout: float) -> tuple[socket.socket, str | None, str]:
    """A connected socket to the running bridge, the token it wants (TCP only), and where it is, for messages."""
    ep = endpoint_path()
    if ep.exists():
        try:
            info = json.loads(ep.read_text(encoding="utf-8"))
            host, port, token = str(info["host"]), int(info["port"]), str(info["token"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BridgeUnavailable(f"can't read the FreeCAD bridge's endpoint file {ep}: {e}") from e
        if host not in LOOPBACK:  # the bridge only ever listens on this machine
            raise BridgeUnavailable(f"{ep} points at {host}, not this machine; delete it and start the bridge again")
        where = f"{host}:{port} (from {ep})"
        try:
            s = socket.create_connection((host, port), timeout=5)
        except (ConnectionRefusedError, socket.timeout) as e:
            raise BridgeUnavailable(f"the FreeCAD bridge isn't running ({e.__class__.__name__} on {where})") from e
        except OSError as e:
            raise BridgeUnavailable(f"can't reach the FreeCAD bridge at {where}: {e}") from e
        s.settimeout(timeout + 5)
        return s, token, where
    if transport() == "tcp":
        raise BridgeUnavailable(f"the FreeCAD bridge isn't running (no {ep})")
    path = socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout + 5)
    try:
        s.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        s.close()
        raise BridgeUnavailable(f"the FreeCAD bridge isn't running ({e.__class__.__name__} on {path})") from e
    except OSError as e:  # e.g. a socket path over the OS limit (104 bytes on macOS)
        s.close()
        raise BridgeUnavailable(f"can't reach the FreeCAD bridge at {path}: {e}") from e
    return s, None, str(path)


def call(op: str, timeout: float = 120, **args) -> dict:
    """Send one request to the bridge and return its reply."""
    s, token, where = _connect(timeout)
    req = {"op": op, "timeout": timeout} | args
    if token is not None:
        req["token"] = token
    try:
        with s, s.makefile("rwb") as f:
            f.write((json.dumps(req) + "\n").encode())
            f.flush()
            line = f.readline()
    except socket.timeout as e:
        raise BridgeUnavailable("the FreeCAD bridge didn't answer; FreeCAD may be busy or showing a dialog") from e
    except OSError as e:  # a reset when the bridge stops mid-call
        raise BridgeUnavailable(f"lost the connection to the FreeCAD bridge at {where}: {e}") from e
    if not line:
        raise BridgeUnavailable("the FreeCAD bridge closed the connection without answering")
    return json.loads(line)


def not_running() -> dict:
    return {
        "ok": False,
        "running": False,
        "start": start_command(),
        "macro": MACRO_NAME,
        "how": "If the user has started the bridge before, they start it again in FreeCAD with Macro > Recent macros > "
               f"{MACRO_NAME} (or Macro > Macros... > {MACRO_NAME} > Execute); the bridge saved that macro the first "
               "time. Otherwise, or if the macro isn't there: open FreeCAD, then View > Panels > Python console, and "
               "paste the start line. The Report view then shows '[cadmate] bridge listening on ...'. "
               "It stops with App._cadmate_bridge.stop().",
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

    The position is kept in a file in cadmate's data folder, so a new cadmate server
    (a new Claude session) carries on where the last one stopped instead of
    missing the edits made in between. With no position yet, every hand edit
    since the bridge started counts.
    """

    def __init__(self, path: Path | None = None):
        self.path = path

    def _file(self) -> Path:
        return self.path or data_dir() / "seen.json"

    def _load(self) -> int:
        try:
            return int(json.loads(self._file().read_text(encoding="utf-8"))["seq"])
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
            self._file().write_text(json.dumps({"seq": r["seq"]}), encoding="utf-8")
        except OSError:
            pass
        return summarize(r["events"])
