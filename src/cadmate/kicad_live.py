"""Work on a footprint or symbol together while it is open in KiCad's editor.

KiCad's footprint and symbol editors have no API, but since KiCad 10 both watch
the file of what they have open: when it changes on disk they reload it about a
second later, without asking, unless the editor holds unsaved edits (then they
ask). So the loop is: the user saves in KiCad and comments, cadmate reads the
file, sees what the user changed since its last look, and writes its edit on
top; KiCad reloads it.

cadmate never regenerates the item. Edits are text replacements inside the
footprint, or inside the one symbol of a library, so everything the user did by
hand stays as KiCad wrote it. Each edit is a step with a label in cadmate's own
history (KiCad's undo list starts over on every reload), and the last step can
be undone on top of whatever the user changed since.

What cadmate last wrote or read is kept per item in cadmate's data folder, so a
new Claude session carries on where the last one stopped.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import apps, check, footprint, live, sexpr, symbol, symbol_check

RAW_DIFF_LINES = 80  # longer diffs are cut; the item changes cover them
HISTORY_KEPT = 50


class LiveError(ValueError):
    pass


def data_dir() -> Path:
    return live.socket_path().parent / "kicad_live"


class Target:
    """One footprint file, or one symbol in a library file."""

    def __init__(self, path: str | Path, name: str | None = None):
        p = Path(path).expanduser().resolve()
        if p.is_dir() and p.suffix == ".kicad_symdir":
            if not name:
                raise LiveError(f"{p} is a symbol folder; give the symbol name")
            p = p / f"{name}.kicad_sym"
        if p.suffix == ".kicad_mod":
            self.kind = "footprint"
        elif p.suffix == ".kicad_sym":
            self.kind = "symbol"
        else:
            raise LiveError("path must be a footprint (.kicad_mod) or a symbol library (.kicad_sym)")
        if not p.is_file():
            raise LiveError(f"{p} doesn't exist")
        self.path = p
        self.name = name
        if self.kind == "symbol" and name is None:
            names = [n.atoms()[0] for n in sexpr.parse(self.read()).children("symbol")]
            if len(names) != 1:
                raise LiveError(f"{p.name} holds {len(names)} symbols; give the symbol name: {names[:20]}")
            self.name = names[0]

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def span(self, text: str) -> tuple[int, int]:
        """Where the item sits in the file's text."""
        root = sexpr.parse(text)
        if self.kind == "footprint":
            if root.head not in ("footprint", "module"):
                raise LiveError(f"{self.path.name} is not a footprint file")
            return root.start, root.end
        if root.head != "kicad_symbol_lib":
            raise LiveError(f"{self.path.name} is not a symbol library")
        for node in root.children("symbol"):
            if node.atoms()[:1] == [self.name]:
                return node.start, node.end
        raise LiveError(f"no symbol {self.name!r} in {self.path.name} any more; was it renamed in KiCad?")

    def item(self, text: str) -> str:
        s, e = self.span(text)
        return text[s:e]

    def replace(self, text: str, item: str) -> str:
        s, e = self.span(text)
        return text[:s] + item + text[e:]

    def parse(self, item: str):
        if self.kind == "footprint":
            return footprint.parse(item)
        return symbol.parse_symbol(item)

    def summary(self, parsed) -> dict:
        return parsed.to_dict()

    def check(self, parsed) -> dict:
        if self.kind == "footprint":
            return check.check_footprint(parsed)
        return symbol_check.check_symbol(parsed)

    def state_file(self) -> Path:
        key = hashlib.sha1(f"{self.path}\0{self.name or ''}".encode()).hexdigest()[:16]
        return data_dir() / f"{key}.json"

    def label(self) -> str:
        return f"{self.path.name}:{self.name}" if self.kind == "symbol" else self.path.name


# --- what cadmate last saw, and its steps ---------------------------------------------------------


def _load(t: Target) -> dict:
    try:
        st = json.loads(t.state_file().read_text())
        if st.get("path") == str(t.path) and st.get("name") == t.name:
            return st
    except (OSError, ValueError):
        pass
    return {"path": str(t.path), "name": t.name, "seen": None, "steps": []}


def _save(t: Target, st: dict) -> None:
    st["steps"] = st["steps"][-HISTORY_KEPT:]
    f = t.state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st))


MAC_SETTLE = 0.5  # seconds between the file leaving its folder and coming back, so KiCad sees both


def _write(t: Target, text: str) -> None:
    """Write the file so that KiCad's editor notices.

    KiCad watches the file's folder through wxWidgets. On Linux (inotify) and Windows a write to the file is reported
    with its path, so writing in place is enough. On macOS wxWidgets uses kqueue, which reports only names appearing in
    or leaving the folder, by comparing listings: a write in place, or a rename over the file, changes no name and goes
    unseen. So there the file is moved aside, and the new one moved in after a pause long enough for KiCad to list the
    folder in between; the old copy is kept until the new one is in place.
    """
    t.parse(t.item(text))  # never hand KiCad a file we can't read back
    if platform.system() != "Darwin":
        t.path.write_text(text, encoding="utf-8")
        return
    new = t.path.with_name(f".{t.path.name}.cadmate-new")
    old = t.path.with_name(f".{t.path.name}.cadmate-old")
    new.write_text(text, encoding="utf-8")
    os.replace(t.path, old)
    try:
        time.sleep(MAC_SETTLE)
    finally:
        os.replace(new, t.path)
    old.unlink(missing_ok=True)


# --- what changed -------------------------------------------------------------------------------


def _r(v):
    return round(v, 4) if isinstance(v, float) else v


def _fields_changed(a: dict, b: dict) -> dict:
    return {k: [_r(a.get(k)), _r(b.get(k))] for k in sorted(set(a) | set(b)) if _r(a.get(k)) != _r(b.get(k))}


def _pair(old: list, new: list, key) -> tuple[list, list, list]:
    """Match items by key, keeping file order among items with the same key. Returns (pairs, removed, added)."""
    groups: dict = {}
    for i in old:
        groups.setdefault(key(i), []).append(i)
    pairs, added = [], []
    for i in new:
        g = groups.get(key(i))
        if g:
            pairs.append((g.pop(0), i))
        else:
            added.append(i)
    removed = [i for g in groups.values() for i in g]
    return pairs, removed, added


def _pad_dict(p: footprint.Pad) -> dict:
    d = asdict(p)
    d["offset"] = list(d["offset"])
    return {k: v for k, v in d.items() if k != "number"}


def _graphic_key(g: footprint.Graphic) -> tuple:
    return (g.kind, g.layer, tuple((round(x, 3), round(y, 3)) for x, y in g.pts), round(g.width, 3), g.filled)


def _graphic_text(g: footprint.Graphic) -> str:
    pts = " ".join(f"({x:g},{y:g})" for x, y in g.pts)
    return f"{g.kind} on {g.layer} {pts}" + (f" width {g.width:g}" if g.width else "")


def diff_footprints(a: footprint.Footprint, b: footprint.Footprint) -> list[dict]:
    out: list[dict] = []
    pairs, removed, added = _pair(a.pads, b.pads, lambda p: p.number)
    for old, new in pairs:
        ch = _fields_changed(_pad_dict(old), _pad_dict(new))
        if ch:
            out.append({"pad": old.number, "changed": ch})
    out += [{"pad": p.number, "removed": True, "was": _pad_dict(p)} for p in removed]
    out += [{"pad": p.number, "added": True, "now": _pad_dict(p)} for p in added]
    ga, gb = {_graphic_key(g): g for g in a.graphics}, {_graphic_key(g): g for g in b.graphics}
    out += [{"graphic": _graphic_text(ga[k]), "removed": True} for k in ga if k not in gb]
    out += [{"graphic": _graphic_text(gb[k]), "added": True} for k in gb if k not in ga]
    # KiCad 10 adds empty Datasheet and Description fields when it saves a footprint that lacks them: not the user's doing.
    ta, tb = {x for x in a.texts if x[1] not in ("", "~")}, {x for x in b.texts if x[1] not in ("", "~")}
    out += [{"text": list(x), "removed": True} for x in a.texts if x in ta and x not in tb]
    out += [{"text": list(x), "added": True} for x in b.texts if x in tb and x not in ta]
    if [asdict(m) for m in a.models] != [asdict(m) for m in b.models]:
        out.append({"models": {"was": [asdict(m) for m in a.models], "now": [asdict(m) for m in b.models]}})
    if a.name != b.name:
        out.append({"name": [a.name, b.name]})
    if a.smd != b.smd:
        out.append({"smd": [a.smd, b.smd]})
    return out


def _pin_dict(p: symbol.Pin) -> dict:
    d = p.to_dict()
    d["rotation"] = p.rotation
    d["unit"], d["style"] = p.unit, p.style
    return {k: v for k, v in d.items() if k not in ("number", "side")}


def diff_symbols(a: symbol.Symbol, b: symbol.Symbol) -> list[dict]:
    out: list[dict] = []
    pairs, removed, added = _pair(a.pins, b.pins, lambda p: (p.unit, p.style, p.number))
    for old, new in pairs:
        ch = _fields_changed(_pin_dict(old), _pin_dict(new))
        if ch:
            out.append({"pin": old.number, "unit": old.unit, "changed": ch})
    out += [{"pin": p.number, "unit": p.unit, "removed": True, "was": _pin_dict(p)} for p in removed]
    out += [{"pin": p.number, "unit": p.unit, "added": True, "now": _pin_dict(p)} for p in added]
    for k in sorted(set(a.properties) | set(b.properties)):
        pa, pb = a.properties.get(k), b.properties.get(k)
        if pa is None or pb is None or vars(pa) != vars(pb):
            out.append({"field": k, "was": vars(pa) if pa else None, "now": vars(pb) if pb else None})
    ra, rb = [vars(r) for r in a.rects], [vars(r) for r in b.rects]
    if ra != rb:
        out.append({"rectangles": {"was": ra, "now": rb}})
    la, lb = [vars(p) for p in a.polylines], [vars(p) for p in b.polylines]
    if la != lb:
        out.append({"polylines": {"was": len(la), "now": len(lb)}})
    if a.other_graphics != b.other_graphics:
        out.append({"other_graphics": [a.other_graphics, b.other_graphics]})
    for k in ("pin_names_offset", "hide_pin_names", "hide_pin_numbers", "power", "units_locked", "extends"):
        if getattr(a, k) != getattr(b, k):
            out.append({k: [getattr(a, k), getattr(b, k)]})
    return out


def _raw_diff(a: str, b: str) -> dict:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "before", "after", n=1, lineterm=""))[2:]
    if len(lines) > RAW_DIFF_LINES:
        return {"raw_diff": "\n".join(lines[:RAW_DIFF_LINES]), "raw_diff_cut": f"{len(lines) - RAW_DIFF_LINES} more lines"}
    return {"raw_diff": "\n".join(lines)}


def changes(t: Target, before: str, after: str) -> dict:
    """What differs between two versions of the item: item by item (pads, pins, fields...), plus the raw text diff."""
    if before == after:
        return {"changed": False, "items": []}
    try:
        a, b = t.parse(before), t.parse(after)
        items = diff_footprints(a, b) if t.kind == "footprint" else diff_symbols(a, b)
    except (ValueError, sexpr.SexprError) as e:
        items = [{"unreadable": str(e)}]
    out = {"changed": True, "items": items} | _raw_diff(before, after)
    if not items:
        out["note"] = "only formatting or details the item list doesn't cover changed; see raw_diff"
    return out


def _overwritten(st: dict, current: str) -> dict | None:
    """The last step's change is gone again: typically KiCad saved over it from a copy that never reloaded it."""
    steps = [s for s in st["steps"] if not s.get("undone")]
    if not steps:
        return None
    last = steps[-1]
    if current in (last["after"], last["before"]):
        return {"step": last["label"]} if current == last["before"] else None
    gone = [e for e in last["edits"] if e["new"] and e["new"] not in current and e["old"] in current]
    if gone and len(gone) == len([e for e in last["edits"] if e["new"]]):
        return {"step": last["label"]}
    return None


def _look(t: Target, st: dict, current: str) -> dict:
    """The user's changes since cadmate last read or wrote the item."""
    if st["seen"] is None:
        out = {"first_look": True}
    else:
        out = changes(t, st["seen"], current)
        if out["changed"]:
            lost = _overwritten(st, current)
            if lost:
                out["overwrote_cadmate_step"] = lost["step"]
                out["note"] = (f"cadmate's last step '{lost['step']}' is no longer in the file: KiCad probably saved over it "
                               "(the editor had unsaved edits when cadmate wrote, and the reload was declined). Ask the user "
                               "before applying it again.")
    st["seen"] = current
    return out


# --- the tools ----------------------------------------------------------------------------------


def _history(st: dict) -> list[dict]:
    return [{"step": i + 1, "label": s["label"], "time": s["time"], "undone": bool(s.get("undone"))} for i, s in enumerate(st["steps"])]


def state(path: str, name: str | None = None) -> dict:
    t = Target(path, name)
    st = _load(t)
    text = t.read()
    current = t.item(text)
    user = _look(t, st, current)
    _save(t, st)
    parsed = t.parse(current)
    return {
        "ok": True,
        "kind": t.kind,
        "item": t.label(),
        "user_changes": user,
        "summary": t.summary(parsed),
        "check": t.check(parsed),
        "text": current,
        "history": _history(st),
    }


def _apply(item: str, edits: list[dict]) -> str:
    out = item
    for i, e in enumerate(edits, 1):
        old, new = e.get("old"), e.get("new")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise LiveError(f"edit {i}: give old (text that is in the item now) and new")
        n = out.count(old)
        if n == 0:
            raise LiveError(f"edit {i}: old text not found in the item as it is now; read it again with kicad_live_state")
        if n > 1 and not e.get("all"):
            raise LiveError(f"edit {i}: old text occurs {n} times; give more context or all: true")
        out = out.replace(old, new) if e.get("all") else out.replace(old, new, 1)
    return out


def _new_findings(before: dict, after: dict) -> dict:
    return {"new_errors": [x for x in after["errors"] if x not in before["errors"]],
            "fixed_errors": [x for x in before["errors"] if x not in after["errors"]],
            "new_warnings": [x for x in after["warnings"] if x not in before["warnings"]]}


def edit(path: str, edits: list[dict], label: str, name: str | None = None) -> dict:
    if not label or not label.strip():
        raise LiveError("give a label: one line saying what this step changes")
    t = Target(path, name)
    st = _load(t)
    text = t.read()
    current = t.item(text)
    user = _look(t, st, current)
    try:
        new_item = _apply(current, edits)
        parsed = t.parse(new_item)
    except (LiveError, ValueError, sexpr.SexprError) as e:
        _save(t, st)
        return {"ok": False, "error": str(e), "user_changes": user}
    if t.kind == "footprint" and parsed.name != t.parse(current).name:
        _save(t, st)
        return {"ok": False, "error": "don't rename the footprint here: KiCad's editor would lose track of it. Use KiCad's Save As.",
                "user_changes": user}
    if t.kind == "symbol" and parsed.name != t.name:
        _save(t, st)
        return {"ok": False, "error": "don't rename the symbol here: KiCad's editor would lose track of it. Rename it in KiCad.",
                "user_changes": user}
    if new_item == current:
        _save(t, st)
        return {"ok": False, "error": "the edits change nothing", "user_changes": user}
    before_check = t.check(t.parse(current))
    after_check = t.check(parsed)
    _write(t, t.replace(text, new_item))
    st["steps"].append({"label": label.strip(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "edits": [{"old": e["old"], "new": e["new"], "all": bool(e.get("all"))} for e in edits],
                        "before": current, "after": new_item})
    st["seen"] = new_item
    _save(t, st)
    return {"ok": True, "written": str(t.path), "step": len(st["steps"]), "label": label.strip(), "user_changes": user,
            "check": {"ok": after_check["ok"], "errors": after_check["errors"]} | _new_findings(before_check, after_check), "changed": changes(t, current, new_item)["items"]}


def undo(path: str, name: str | None = None) -> dict:
    t = Target(path, name)
    st = _load(t)
    text = t.read()
    current = t.item(text)
    user = _look(t, st, current)
    live_steps = [s for s in st["steps"] if not s.get("undone")]
    if not live_steps:
        _save(t, st)
        return {"ok": False, "error": "no cadmate step to undo", "user_changes": user}
    step = live_steps[-1]
    if current == step["after"]:
        reverted = step["before"]
    else:
        # The user changed things since: take back only this step's replacements, in reverse order.
        try:
            reverted = _apply(current, [{"old": e["new"], "new": e["old"], "all": e["all"]} for e in reversed(step["edits"]) if e["new"]])
            if any(not e["new"] for e in step["edits"]):
                raise LiveError("the step deleted text, and the user has changed the item since, so cadmate can't tell where it went")
        except LiveError as e:
            _save(t, st)
            return {"ok": False, "error": f"can't undo '{step['label']}' on top of the user's later changes: {e}. Make the reverse change "
                                          "as a new edit instead.", "user_changes": user}
    _write(t, t.replace(text, reverted))
    step["undone"] = True
    st["seen"] = reverted
    _save(t, st)
    return {"ok": True, "undid": step["label"], "written": str(t.path), "user_changes": user, "changed": changes(t, current, reverted)["items"]}


# --- pictures -----------------------------------------------------------------------------------


def _pixel_size(svg: Path, width: int, square: bool = False, margin: float = 0.04) -> None:
    """Give the SVG a size in pixels, width wide, with a margin round its viewBox (kicad-cli's box clips some text).
    kicad-cli sizes it in mm, and macOS's qlmanage draws it at that physical size in a corner instead of scaling it;
    qlmanage also always makes a square picture, so square=True centres the drawing in a square box."""
    text = svg.read_text(encoding="utf-8")
    root = re.search(r"<svg\b[^>]*>", text)
    box = re.search(r'viewBox="\s*([-\d.]+)[\s,]+([-\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"', root.group(0)) if root else None
    if not box or float(box.group(3)) <= 0 or float(box.group(4)) <= 0:
        return
    x, y, w, h = (float(v) for v in box.groups())
    pad = margin * max(w, h)
    x, y, w, h = x - pad, y - pad, w + 2 * pad, h + 2 * pad
    if square:
        side = max(w, h)
        x, y, w, h = x - (side - w) / 2, y - (side - h) / 2, side, side
    height = max(1, round(width * h / w))
    tag = re.sub(r'\s(width|height|viewBox)="[^"]*"', "", root.group(0))
    tag = tag.replace("<svg", f'<svg width="{width}" height="{height}" viewBox="{x:.4f} {y:.4f} {w:.4f} {h:.4f}"', 1)
    svg.write_text(text[:root.start()] + tag + text[root.end():], encoding="utf-8")


def _svg_to_png(svg: Path, png: Path, width: int) -> None:
    quicklook = not shutil.which("rsvg-convert") and platform.system() == "Darwin"
    try:
        import cairosvg  # noqa: F401  optional
        quicklook = False
    except ImportError:
        pass
    _pixel_size(svg, width, square=quicklook)
    try:
        import cairosvg  # optional

        cairosvg.svg2png(url=str(svg), write_to=str(png), output_width=width, background_color="white")
        return
    except ImportError:
        pass
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-w", str(width), "-b", "white", "-o", str(png), str(svg)], check=True, timeout=60)
        return
    if platform.system() == "Darwin" and shutil.which("qlmanage"):
        subprocess.run(["qlmanage", "-t", "-s", str(width), "-o", str(png.parent), str(svg)], check=True, timeout=60,
                       capture_output=True)
        made = png.parent / (svg.name + ".png")
        if made.exists():
            made.replace(png)
            return
    raise LiveError("no SVG to PNG converter found; install librsvg (rsvg-convert) or the cairosvg Python package")


def snapshot(path: str, name: str | None = None, width: int = 900) -> Path:
    """Render the item as saved, with kicad-cli, to a PNG. Returns its path."""
    t = Target(path, name)
    cli = apps.find_kicad_cli()
    if not cli:
        raise LiveError(f"kicad-cli not found; set {apps.KICAD_CLI_ENV}")
    out = Path(tempfile.mkdtemp(prefix="cadmate-snap-"))
    if t.kind == "footprint":
        # Pad outlines and numbers on the fab layer, so the picture shows which pad is which.
        cmd = [cli, "fp", "export", "svg", "--footprint", t.path.stem, "--sketch-pads-on-fab-layers", "--output", str(out), str(t.path.parent)]
    else:
        cmd = [cli, "sym", "export", "svg", "--symbol", t.name, "--output", str(out), str(t.path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    svgs = sorted(out.glob("*.svg"))
    if not svgs:
        raise LiveError(f"kicad-cli made no picture (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}")
    png = out / (svgs[0].stem + ".png")
    _svg_to_png(svgs[0], png, width)
    return png
