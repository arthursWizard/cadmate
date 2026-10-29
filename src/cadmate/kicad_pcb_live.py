"""Work on a board together in the user's open KiCad PCB editor, through KiCad's API.

The API (kicad-python, "kipy") is served by the PCB editor once the user ticks
"Enable KiCad API" in KiCad's preferences (Plugins page). cadmate reads the
board as it is in the editor, saved or not, and edits it inside a commit, so
each edit is one undo step named "cadmate: <label>" in KiCad's own undo list.

The API has no change log, so before each edit cadmate reads the board as a
whole (KiCad's file text, straight from the editor), indexes its items by
their uuid, and compares them with what it saw after its own last edit:
whatever differs is the user's doing. The index is kept per board in cadmate's
data folder, so a new Claude session carries on where the last one stopped.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import traceback
from pathlib import Path

from . import apps, live, sexpr

TIMEOUT_MS = 20000  # reading a large board as text takes a while
LISTED_CHANGES = 60  # changes beyond this are counted, not listed
UNDO_ACTION = "common.Interactive.undo"
REDO_ACTION = "common.Interactive.redo"

ENABLE_HOW = ("Ask the user to turn on KiCad's API: KiCad menu > Settings… (Preferences on Linux and Windows) > Plugins, "
              "tick \"Enable KiCad API\", then restart KiCad and open the board in the PCB editor.")


class PcbUnavailable(RuntimeError):
    pass


def _kipy():
    try:
        import kipy
        import kipy.errors
    except ImportError as e:  # pragma: no cover - a dependency
        raise PcbUnavailable("the kicad-python package is missing; reinstall cadmate") from e
    return kipy


def _other_sockets() -> list[str]:
    """KiCad's API sockets besides the default one. The project manager serves the default socket; a PCB editor
    started on its own (opening a .kicad_pcb directly) serves api-<pid>.sock next to it. Windows uses named pipes,
    which this can't list, so there it finds nothing."""
    import glob
    import os

    from kipy.kicad import _default_socket_path

    if os.environ.get("KICAD_API_SOCKET"):
        return []
    path = _default_socket_path().removeprefix("ipc://")
    found = sorted(glob.glob(os.path.join(os.path.dirname(path), "api-*.sock")), key=os.path.getmtime, reverse=True)
    return [f"ipc://{f}" for f in found]


def connect(timeout_ms: int = TIMEOUT_MS):
    """KiCad and the board open in its PCB editor, as kipy objects."""
    kipy = _kipy()
    reached, last = False, None
    for sock in [None] + _other_sockets():
        try:
            kicad = kipy.KiCad(socket_path=sock, timeout_ms=timeout_ms)
            kicad.ping()
        except Exception as e:
            last = last or e
            continue
        reached = True
        try:
            return kicad, kicad.get_board()
        except Exception as e:
            last = e
    if not reached:
        raise PcbUnavailable(f"can't reach KiCad's API ({last.__class__.__name__}: {last}). {ENABLE_HOW}") from last
    raise PcbUnavailable(f"KiCad is running but has no board open in the PCB editor ({last})") from last


def busy_hint(e: Exception) -> str:
    text = str(e)
    if "busy" in text.lower() or "AS_BUSY" in text:
        return f"{text}. KiCad is busy: a tool or dialog is active in the PCB editor. Ask the user to finish it (Esc), then try again."
    return text


# --- reading the board ------------------------------------------------------------------------------


_GRAPHICS = ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve", "gr_text", "gr_text_box", "gr_bbox")
_TRACKED = ("footprint", "segment", "arc", "via", "zone", "dimension", "group", "image", "table", "target") + _GRAPHICS


def _f(v: str) -> float | str:
    try:
        return round(float(v), 4)
    except ValueError:
        return v


def _at(node: sexpr.Node | None) -> list:
    return [_f(a) for a in node.atoms()] if node is not None else []


def _layer(node: sexpr.Node) -> str | list | None:
    lay = node.child("layer")
    if lay is not None and lay.atoms():
        return lay.atoms()[0]
    lays = node.child("layers")
    return lays.atoms() if lays is not None else None


def _uuid(node: sexpr.Node) -> str | None:
    for key in ("uuid", "tstamp"):
        u = node.child(key)
        if u is not None and u.atoms():
            return u.atoms()[0]
    return None


def _net(node: sexpr.Node, names: dict[str, str]) -> str | None:
    n = node.child("net")
    if n is None or not n.atoms():
        return None
    a = n.atoms()
    if len(a) >= 2:
        return a[1]
    return names.get(a[0], a[0])  # KiCad 9 and older: a number declared at the top; KiCad 10: the name


def _prop(node: sexpr.Node, key: str) -> str | None:
    for p in node.children("property"):
        a = p.atoms()
        if len(a) >= 2 and a[0] == key:
            return a[1]
    for t in node.children("fp_text"):  # KiCad 7 and older
        a = t.atoms()
        if len(a) >= 2 and a[0] == key.lower():
            return a[1]
    return None


def _summary(node: sexpr.Node, names: dict[str, str]) -> dict:
    h = node.head
    if h == "footprint":
        at = _at(node.child("at"))
        return {"type": "footprint", "ref": _prop(node, "Reference"), "value": _prop(node, "Value"), "lib": (node.atoms() or [""])[0],
                "x": at[0] if at else 0.0, "y": at[1] if len(at) > 1 else 0.0, "rotation": at[2] if len(at) > 2 else 0.0,
                "side": "back" if _layer(node) == "B.Cu" else "front", "locked": "locked" in node.atoms() or node.child("locked") is not None}
    if h in ("segment", "arc"):
        d = {"type": "track" if h == "segment" else "arc track", "start": _at(node.child("start"))[:2], "end": _at(node.child("end"))[:2],
             "width": (_at(node.child("width")) or [None])[0], "layer": _layer(node), "net": _net(node, names)}
        if h == "arc":
            d["mid"] = _at(node.child("mid"))[:2]
        return d
    if h == "via":
        return {"type": "via", "at": _at(node.child("at"))[:2], "size": (_at(node.child("size")) or [None])[0],
                "drill": (_at(node.child("drill")) or [None])[0], "layers": _layer(node), "net": _net(node, names)}
    if h == "zone":
        name = node.child("name")
        pts = node.child("polygon")
        corners = len(pts.child("pts").children("xy")) if pts is not None and pts.child("pts") is not None else 0
        net_name = node.child("net_name")
        net = _net(node, names) or (net_name.atoms()[0] if net_name is not None and net_name.atoms() else None)
        return {"type": "zone", "net": net,
                "name": name.atoms()[0] if name is not None and name.atoms() else None, "layer": _layer(node), "corners": corners}
    d = {"type": h.removeprefix("gr_") if h in _GRAPHICS else h, "layer": _layer(node)}
    for key in ("start", "end", "center", "mid", "at"):
        if node.child(key) is not None:
            d[key] = _at(node.child(key))[:3]
    if h in ("gr_text", "gr_text_box") and node.atoms():
        d["text"] = node.atoms()[0]
    return d


def index(board_text: str) -> dict[str, dict]:
    """The board's items by uuid: {uuid: {"hash", "summary"}}. The hash covers everything in the item."""
    root = sexpr.parse(board_text)
    names = {}
    for n in root.children("net"):
        a = n.atoms()
        if len(a) >= 2:
            names[a[0]] = a[1]
    out = {}
    for node in root.items:
        if not isinstance(node, sexpr.Node) or node.head not in _TRACKED:
            continue
        text = board_text[node.start:node.end]
        key = _uuid(node) or f"{node.head}:{hashlib.sha1(text.encode()).hexdigest()[:12]}"
        out[key] = {"hash": hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:16], "summary": _summary(node, names)}
    return out


def _label(s: dict) -> str:
    if s["type"] == "footprint":
        return f"footprint {s['ref'] or '?'}"
    if s.get("net"):
        return f"{s['type']} on {s['net']}"
    return s["type"]


def diff(before: dict[str, dict], after: dict[str, dict]) -> dict:
    """What differs between two board indexes, item by item."""
    items = []
    for k, a in after.items():
        b = before.get(k)
        if b is None:
            items.append({"item": _label(a["summary"]), "added": True, "now": a["summary"]})
        elif b["hash"] != a["hash"]:
            changed = {f: [b["summary"].get(f), v] for f, v in a["summary"].items() if b["summary"].get(f) != v}
            entry = {"item": _label(a["summary"]), "changed": changed}
            if not changed:
                entry["note"] = "details inside it changed (fields, pads, text placement or style)"
            items.append(entry)
    for k, b in before.items():
        if k not in after:
            items.append({"item": _label(b["summary"]), "removed": True, "was": b["summary"]})
    out = {"changed": bool(items), "count": len(items), "items": items[:LISTED_CHANGES]}
    if len(items) > LISTED_CHANGES:
        kinds: dict[str, int] = {}
        for i in items[LISTED_CHANGES:]:
            kinds[i["item"]] = kinds.get(i["item"], 0) + 1
        out["more"] = kinds
    return out


def digest(idx: dict[str, dict]) -> str:
    """One hash for the whole board index: equal digests, same board."""
    return hashlib.sha1("\n".join(f"{k} {v['hash']}" for k, v in sorted(idx.items())).encode()).hexdigest()[:16]


def _touched(before: dict[str, dict], after: dict[str, dict]) -> set[str]:
    return {k for k in before.keys() | after.keys() if (before.get(k) or {}).get("hash") != (after.get(k) or {}).get("hash")}


# --- what cadmate last saw ----------------------------------------------------------------------------


def _state_file(board_name: str, project: str = "") -> Path:
    key = hashlib.sha1(f"{project}\0{board_name}".encode()).hexdigest()[:16]
    return live.socket_path().parent / "kicad_pcb" / f"{key}.json"


def _load(f: Path) -> dict:
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {"seen": None, "steps": []}


def _save(f: Path, st: dict) -> None:
    st["steps"] = st["steps"][-50:]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st))


class Session:
    def __init__(self, board, kicad):
        self.board, self.kicad = board, kicad
        doc = board.document
        project = getattr(getattr(doc, "project", None), "path", "") or ""
        self.file = _state_file(board.name, project)
        self.st = _load(self.file)

    def snapshot(self) -> dict[str, dict]:
        return index(self.board.get_as_string())

    def look(self) -> tuple[dict, dict]:
        """(the board's index now, the user's changes since cadmate last looked)."""
        now = self.snapshot()
        user = {"first_look": True} if self.st["seen"] is None else diff(self.st["seen"], now)
        self.st["seen"] = now
        _save(self.file, self.st)
        return now, user


# --- the tools ------------------------------------------------------------------------------------------


def _selection(board, now: dict) -> list[dict]:
    try:
        items = board.get_selection()
    except Exception:
        return []
    out = []
    for it in items:
        uid = getattr(getattr(it, "id", None), "value", None)
        if uid in now:
            out.append(now[uid]["summary"])
        else:
            out.append({"type": type(it).__name__})
    return out


def state(footprints: bool = True, tracks_on_nets: list[str] | None = None) -> dict:
    kicad, board = connect()
    s = Session(board, kicad)
    now, user = s.look()
    by_type: dict[str, int] = {}
    for v in now.values():
        by_type[v["summary"]["type"]] = by_type.get(v["summary"]["type"], 0) + 1
    out = {"ok": True, "board": board.name, "user_changes": user, "selection": _selection(board, now), "counts": by_type,
           "history": [st["label"] for st in s.st["steps"]], "units": "mm, KiCad's board frame (Y down), rotation in degrees"}
    if footprints:
        out["footprints"] = sorted((v["summary"] for v in now.values() if v["summary"]["type"] == "footprint"),
                                   key=lambda f: f["ref"] or "")
    if tracks_on_nets:
        wanted = set(tracks_on_nets)
        out["tracks"] = [v["summary"] for v in now.values() if v["summary"].get("net") in wanted and v["summary"]["type"] != "zone"]
    return out


def _namespace(kicad, board) -> dict:
    kipy = _kipy()
    from kipy.board_types import BoardLayer
    from kipy.geometry import Angle, Vector2
    from kipy.util.units import from_mm, to_mm

    def footprint(ref: str):
        hits = [f for f in board.get_footprints() if f.reference_field.text.value == ref]
        if not hits:
            raise KeyError(f"no footprint {ref!r} on the board")
        return hits[0]

    def place(ref: str, x: float | None = None, y: float | None = None, rotation: float | None = None):
        """Move a footprint to x, y (mm) and/or set its rotation (degrees)."""
        f = footprint(ref)
        if x is not None or y is not None:
            p = f.position
            f.position = Vector2.from_xy(from_mm(x) if x is not None else p.x, from_mm(y) if y is not None else p.y)
        if rotation is not None:
            f.orientation = Angle.from_degrees(rotation)
        board.update_items(f)
        return f

    return {"kicad": kicad, "board": board, "kipy": kipy, "Vector2": Vector2, "Angle": Angle, "BoardLayer": BoardLayer,
            "mm": from_mm, "to_mm": to_mm, "footprint": footprint, "place": place, "math": math, "result": None}


def edit(code: str, label: str) -> dict:
    def run(kicad, board):
        ns = _namespace(kicad, board)
        exec(compile(code, "<cadmate edit>", "exec"), ns)
        return ns.get("result")

    return commit(label, run)


def commit(label: str, fn) -> dict:
    """Run fn(kicad, board) inside one commit, so it is one undo step named "cadmate: <label>", and track it like an edit."""
    if not label or not label.strip():
        return {"ok": False, "error": "give a label: one line saying what this step changes"}
    kicad, board = connect()
    s = Session(board, kicad)
    before, user = s.look()
    try:
        c = board.begin_commit()
    except Exception as e:
        return {"ok": False, "error": busy_hint(e), "user_changes": user}
    try:
        result = fn(kicad, board)
    except Exception as e:
        try:
            board.drop_commit(c)
        except Exception:
            pass
        return {"ok": False, "error": f"{e.__class__.__name__}: {busy_hint(e)}", "traceback": traceback.format_exc(limit=4)[-2000:],
                "undone": "nothing was changed on the board", "user_changes": user}
    board.push_commit(c, f"cadmate: {label.strip()}")
    after = s.snapshot()
    s.st["seen"] = after
    s.st["steps"].append({"label": label.strip(), "before": digest(before), "after": digest(after),
                          "items": sorted(_touched(before, after))})
    _save(s.file, s.st)
    try:
        json.dumps(result)
    except TypeError:
        result = repr(result)
    return {"ok": True, "label": label.strip(), "user_changes": user, "changed": diff(before, after), "result": result}


def undo() -> dict:
    """Undo cadmate's last step with KiCad's own undo, but only when the board is exactly as that step left it.

    KiCad's undo takes back whatever is on top of its list, so any change after cadmate's step, the user's or not,
    and whether or not cadmate has reported it already, means KiCad's undo would take back that change instead.
    """
    kicad, board = connect()
    s = Session(board, kicad)
    if not s.st["steps"]:
        return {"ok": False, "error": "cadmate has no step on this board to undo"}
    step = s.st["steps"][-1]
    now, user = s.look()
    if "after" not in step or digest(now) != step["after"]:
        return {"ok": False, "error": f"the board changed after cadmate's step '{step['label']}', so KiCad's undo would take back that later "
                                      "change first. Make the reverse change as a new edit, or ask the user to undo in KiCad.",
                "user_changes": user}
    try:
        kicad.run_action(UNDO_ACTION)
    except Exception as e:
        return {"ok": False, "error": busy_hint(e)}
    after = s.snapshot()
    ch = diff(now, after)
    if not ch["changed"]:
        return {"ok": False, "error": "KiCad's undo changed nothing; the PCB editor may not have had focus. Ask the user to undo in KiCad."}
    if digest(after) != step["before"] and not _touched(now, after) <= set(step["items"]):
        try:
            kicad.run_action(REDO_ACTION)
        except Exception:
            pass
        s.st["seen"] = s.snapshot()
        _save(s.file, s.st)
        return {"ok": False, "error": f"KiCad's undo took back something other than '{step['label']}', so cadmate redid it. "
                                      "Make the reverse change as a new edit instead.", "undo_changed": ch}
    s.st["steps"].pop()
    s.st["seen"] = after
    _save(s.file, s.st)
    return {"ok": True, "undid": step["label"], "changed": ch}


def save() -> dict:
    kicad, board = connect()
    board.save()
    return {"ok": True, "saved": board.name}


def snapshot(layers: str = "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Fab,Edge.Cuts", width: int = 1400) -> Path:
    """A picture of the board as it is in the editor (unsaved edits included), from a copy saved by KiCad."""
    from .kicad_live import LiveError, _svg_to_png

    kicad, board = connect()
    cli = apps.find_kicad_cli()
    if not cli:
        raise LiveError(f"kicad-cli not found; set {apps.KICAD_CLI_ENV}")
    out = Path(tempfile.mkdtemp(prefix="cadmate-pcb-"))
    copy = out / "board.kicad_pcb"
    board.save_as(str(copy), overwrite=True, include_project=False)
    svg = out / "board.svg"
    proc = subprocess.run([cli, "pcb", "export", "svg", "--layers", layers, "--mode-single", "--fit-page-to-board",
                           "--exclude-drawing-sheet", "--output", str(svg), str(copy)], capture_output=True, text=True, timeout=180)
    if not svg.exists():
        raise LiveError(f"kicad-cli made no picture (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}")
    png = out / "board.png"
    _svg_to_png(svg, png, width)
    return png
