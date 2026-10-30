"""Review a schematic's readability: what a reviewer would mark up before checking the circuit itself.

KiCad's ERC checks connectivity; nothing in KiCad checks whether a sheet can be read. This review does:
text that collides with other text or with symbols, symbols drawn on top of each other, things off the
sheet or under the title block, four-way junctions, ground symbols pointing up, and how much of the
sheet is joined by net labels rather than wires. The thresholds are cadmate's own heuristics and the
output says so; the drafting conventions they come from are cited.

Coordinates are KiCad's schematic frame: mm, Y down. Text sizes are estimated from the font size, since
KiCad's stroke font has no metrics file; widths are approximate (about ±15%).
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import apps, sexpr

SOURCES = {
    "kicad_ssg": "KiCad Library Conventions S4.2 (klc.kicad.org/symbol/s4/s4.2/) place a symbol's inputs left, outputs "
                 "right, positive supplies top and negative supplies and ground bottom; cadmate applies the same direction "
                 "to the sheet so it reads like the circuit's voltage, top to bottom.",
    "junctions": "Common drafting convention, not from a standard cadmate can cite: avoid four-way junctions, since a "
                 "junction dot lost in printing or zooming turns them into an ambiguous crossing; offset them into two T "
                 "junctions.",
    "readability": "Project rule (cadmate schematic style): prefer wires inside a functional block, labels only where a "
                   "net leaves the block; text must not overlap; everything on the sheet.",
    "kicad_erc": "KiCad's own electrical rules check (kicad-cli sch erc), with the project's rules.",
    "heuristic": "cadmate heuristic: no standard sets this number. Debatable; adjust it to the design.",
}

# Paper sizes, mm (KiCad's page sizes; landscape unless the file says portrait).
PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
         "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
         "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4)}
BORDER = 10.0          # KiCad's default page margin, mm
TITLE_BLOCK = (110.0, 32.0)  # approximate size of KiCad's default title block, mm (bottom right)
CHAR_W = 0.75          # stroke-font advance per mm of font size, approximate
STUB_MAX = 7.62        # a wire this short from a pin to a label is a label stub, mm
LISTED = 60

LIMITS = {
    # Share of two-terminal parts (R, C, L, D, F, Y ...) joined to their neighbours only through labels.
    # Above this the sheet reads as a netlist, not a drawing.
    "passive_label_share": 0.5,
}

_TWO_TERMINAL = ("R", "C", "L", "D", "F", "Y", "FB", "LED")


class ReviewError(RuntimeError):
    pass


def _f(rule, severity, what, why, source, x=None, y=None, refs=None, **values):
    out = {"rule": rule, "severity": severity, "what": what, "why": why, "source": source}
    where = {k: v for k, v in (("x", None if x is None else round(x, 2)), ("y", None if y is None else round(y, 2)),
                                ("refs", refs)) if v is not None}
    if where:
        out["where"] = where
    if values:
        out["values"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in values.items()}
    return out


# --- reading ------------------------------------------------------------------------------------------

Box = tuple[float, float, float, float]


@dataclass
class Text:
    kind: str     # field, label, text
    owner: str    # reference or net name
    text: str
    box: Box


@dataclass
class Placed:
    ref: str
    lib_id: str
    value: str
    power: bool
    at: tuple[float, float]
    rot: int
    body: Box | None
    pins: list[tuple[str, float, float]] = field(default_factory=list)


@dataclass
class Sheet:
    name: str
    paper: tuple[float, float]
    parts: list[Placed]
    texts: list[Text]
    wires: list[tuple[tuple[float, float], tuple[float, float]]]
    labels: list[tuple[str, float, float]]
    junctions: list[tuple[float, float]]


def _floats(n, head):
    c = n.child(head) if n is not None else None
    return c.floats() if c is not None else []


def _effects(node) -> tuple[float, str, str, bool]:
    """(font size, horizontal justify, vertical justify, hidden) of a node's effects."""
    size, hj, vj, hidden = 1.27, "center", "center", False
    e = node.child("effects")
    if e is not None:
        f = e.child("font")
        if f is not None and f.child("size") is not None:
            size = f.child("size").floats()[1]
        j = e.child("justify")
        if j is not None:
            a = j.atoms()
            hj = "left" if "left" in a else ("right" if "right" in a else "center")
            vj = "top" if "top" in a else ("bottom" if "bottom" in a else "center")
        if "hide" in e.atoms() or (e.child("hide") is not None and "yes" in e.child("hide").atoms()):
            hidden = True
    if node.child("hide") is not None and "yes" in node.child("hide").atoms():
        hidden = True
    return size, hj, vj, hidden


def _text_box(text: str, x: float, y: float, angle: float, size: float, hj: str, vj: str) -> Box:
    lines = text.split("\n")
    w = max(len(s) for s in lines) * CHAR_W * size
    h = size * (1 + 1.6 * (len(lines) - 1))
    vertical = round(angle) % 180 == 90
    if vertical:  # KiCad draws 90° text bottom to top; justify runs along the text
        along0 = {"left": 0.0, "center": -w / 2, "right": -w}[hj]
        across0 = {"top": 0.0, "center": -h / 2, "bottom": -h}[vj]
        return (x + across0, y - along0 - w, x + across0 + h, y - along0)
    x0 = x + {"left": 0.0, "center": -w / 2, "right": -w}[hj]
    y0 = y + {"top": 0.0, "center": -h / 2, "bottom": -h}[vj]
    return (x0, y0, x0 + w, y0 + h)


def _xf(px: float, py: float, at, rot: int, mirror: str) -> tuple[float, float]:
    """Library point (Y up) to sheet point (Y down) for a symbol placed at `at`, rotated `rot` (ccw), mirrored.

    Exact for rotation alone and for mirror alone. For a symbol both rotated by 90/270 and mirrored the order
    of the two isn't yet checked against KiCad; a wrong order swaps that symbol's box left/right.
    """
    x, y = px, -py
    if mirror == "y":
        x = -x
    elif mirror == "x":
        y = -y
    for _ in range((rot // 90) % 4):
        x, y = y, -x
    return at[0] + x, at[1] + y


def _lib_geometry(lib: sexpr.Node):
    """(body points, pins) of unit 1 and the common unit, library coordinates."""
    pts, pins = [], []
    for sub in lib.children("symbol"):
        name = sub.atoms()[0] if sub.atoms() else ""
        unit = name.rsplit("_", 2)[-2] if name.count("_") >= 2 else "1"
        if unit not in ("0", "1"):
            continue
        for g in sub.items:
            if not isinstance(g, sexpr.Node):
                continue
            if g.head == "rectangle":
                pts += [tuple(_floats(g, "start")[:2]), tuple(_floats(g, "end")[:2])]
            elif g.head in ("polyline", "bezier"):
                pl = g.child("pts")
                pts += [tuple(p.floats()[:2]) for p in (pl.children("xy") if pl is not None else [])]
            elif g.head == "circle":
                c, r = _floats(g, "center"), _floats(g, "radius")
                if c and r:
                    pts += [(c[0] - r[0], c[1] - r[0]), (c[0] + r[0], c[1] + r[0])]
            elif g.head == "arc":
                pts += [tuple(_floats(g, k)[:2]) for k in ("start", "mid", "end") if _floats(g, k)]
            elif g.head == "pin":
                at = _floats(g, "at")
                num = g.child("number")
                hidden = g.child("hide") is not None or "hide" in g.atoms()
                pins.append((num.atoms()[0] if num is not None and num.atoms() else "", at[0], at[1], hidden))
    return pts, pins


def read(path: str | Path) -> Sheet:
    f = Path(path).expanduser()
    if not f.exists():
        raise ReviewError(f"no schematic at {f}")
    root = sexpr.parse(f.read_text(errors="replace"))
    if root.head != "kicad_sch":
        raise ReviewError(f"{f.name} is not a KiCad schematic")
    pnode = root.child("paper")
    paper = PAPER.get(pnode.atoms()[0] if pnode is not None and pnode.atoms() else "A4", PAPER["A4"])
    if pnode is not None and "portrait" in pnode.atoms():
        paper = (paper[1], paper[0])
    if pnode is not None and pnode.atoms() and pnode.atoms()[0] == "User":
        nums = [float(a) for a in pnode.atoms()[1:3]]
        paper = (nums[0], nums[1])
    libs = {}
    lsn = root.child("lib_symbols")
    for s in (lsn.children("symbol") if lsn is not None else []):
        libs[s.atoms()[0]] = (s, _lib_geometry(s), s.child("power") is not None)
    parts, texts = [], []
    for s in root.children("symbol"):
        lib_id = (s.child("lib_id").atoms() or [""])[0] if s.child("lib_id") is not None else ""
        at = _floats(s, "at") + [0.0, 0.0, 0.0]
        rot = int(round(at[2])) % 360
        mirror = (s.child("mirror").atoms() or [""])[0] if s.child("mirror") is not None else ""
        props = {}
        for p in s.children("property"):
            a = p.atoms()
            if len(a) >= 2:
                props[a[0]] = (a[1], p)
        ref = props.get("Reference", ("?", None))[0]
        value = props.get("Value", ("", None))[0]
        lib = libs.get(lib_id)
        power = bool(lib and lib[2]) or lib_id.startswith("power:") or ref.startswith("#")
        body = None
        pins = []
        if lib:
            bpts, lpins = lib[1]
            sp = [_xf(x, y, at, rot, mirror) for x, y in bpts]
            if sp:
                body = (min(p[0] for p in sp), min(p[1] for p in sp), max(p[0] for p in sp), max(p[1] for p in sp))
            for num, x, y, hidden in lpins:
                px, py = _xf(x, y, at, rot, mirror)
                pins.append((num, round(px, 3), round(py, 3)))
        parts.append(Placed(ref, lib_id, value, power, (at[0], at[1]), rot, body, pins))
        for name, (val, p) in props.items():
            size, hj, vj, hidden = _effects(p)
            if hidden or not val or val == "~":
                continue
            pa = _floats(p, "at") + [0.0, 0.0, 0.0]
            texts.append(Text("field", ref, val, _text_box(val, pa[0], pa[1], pa[2], size, hj, vj)))
    labels = []
    for head in ("label", "global_label", "hierarchical_label"):
        for lab in root.children(head):
            name = lab.atoms()[0] if lab.atoms() else ""
            at = _floats(lab, "at") + [0.0, 0.0, 0.0]
            size, hj, vj, _ = _effects(lab)
            labels.append((name, at[0], at[1]))
            angle = at[2]
            # a label's text runs away from its connection point, sitting just above the wire
            w = len(name) * CHAR_W * size + (size if head != "label" else 0.0)
            pad = 0.3
            if round(angle) % 360 == 0:
                box = (at[0], at[1] - size - pad, at[0] + w, at[1] - pad)
            elif round(angle) % 360 == 180:
                box = (at[0] - w, at[1] - size - pad, at[0], at[1] - pad)
            elif round(angle) % 360 == 90:
                box = (at[0] - size - pad, at[1] - w, at[0] - pad, at[1])
            else:
                box = (at[0] - size - pad, at[1], at[0] - pad, at[1] + w)
            texts.append(Text("label", name, name, box))
    for t in root.children("text"):
        val = t.atoms()[0] if t.atoms() else ""
        at = _floats(t, "at") + [0.0, 0.0, 0.0]
        size, hj, vj, hidden = _effects(t)
        if not hidden and val:
            texts.append(Text("text", "note", val, _text_box(val, at[0], at[1], at[2], size, hj, vj)))
    wires = []
    for w in root.children("wire"):
        pl = w.child("pts")
        xy = [tuple(p.floats()[:2]) for p in (pl.children("xy") if pl is not None else [])]
        wires += [(xy[i], xy[i + 1]) for i in range(len(xy) - 1)]
    junctions = [tuple(_floats(j, "at")[:2]) for j in root.children("junction")]
    return Sheet(f.name, paper, parts, texts, wires, labels, junctions)


# --- checks -------------------------------------------------------------------------------------------


def _overlap(a: Box, b: Box, margin: float = 0.1) -> bool:
    return a[0] < b[2] - margin and b[0] < a[2] - margin and a[1] < b[3] - margin and b[1] < a[3] - margin


def _on(p, a, b, tol=1e-3) -> bool:
    (x, y), (x0, y0), (x1, y1) = p, a, b
    if abs(x0 - x1) < tol and abs(x - x0) < tol:
        return min(y0, y1) - tol <= y <= max(y0, y1) + tol
    if abs(y0 - y1) < tol and abs(y - y0) < tol:
        return min(x0, x1) - tol <= x <= max(x0, x1) + tol
    return False


def _key(p):
    return (round(p[0], 2), round(p[1], 2))


class Review:
    def __init__(self, sheet: Sheet, limits: dict):
        self.s, self.lim = sheet, limits
        self.findings: list[dict] = []
        self.checked: list[str] = []
        self.stats: dict = {}

    def text_overlaps(self):
        self.checked.append("text_overlap")
        t = self.s.texts
        bodies = [(p.ref, p.body) for p in self.s.parts if p.body and not p.power]
        hits = []
        for i, a in enumerate(t):
            for b in t[i + 1:]:
                if _overlap(a.box, b.box):
                    hits.append((a, f"'{a.text}' ({a.owner}) overlaps '{b.text}' ({b.owner})"))
            for ref, box in bodies:
                if a.kind == "field" and a.owner == ref:
                    continue  # a part's own value drawn inside its body (resistors) is KiCad's style
                if _overlap(a.box, box):
                    hits.append((a, f"'{a.text}' ({a.owner}) runs over the symbol {ref}"))
        for a, what in hits[:40]:
            cx, cy = (a.box[0] + a.box[2]) / 2, (a.box[1] + a.box[3]) / 2
            self.findings.append(_f("text_overlap", "warning", what, "Overlapping text can't be read and hides which "
                                    "net or part is meant. Move the field or the part.", "readability", cx, cy))
        if len(hits) > 40:
            self.findings.append(_f("text_overlap", "warning", f"{len(hits) - 40} more text overlaps not listed",
                                    "See above.", "readability"))
        self.stats["text_overlaps"] = len(hits)

    def body_overlaps(self):
        self.checked.append("symbol_overlap")
        ps = [p for p in self.s.parts if p.body and not p.power]
        n = 0
        for i, a in enumerate(ps):
            for b in ps[i + 1:]:
                if _overlap(a.body, b.body):
                    n += 1
                    self.findings.append(_f("symbol_overlap", "warning", f"{a.ref} and {b.ref} are drawn on top of each other",
                                            "Overlapping symbols hide pins and can join wires by accident.", "readability",
                                            a.at[0], a.at[1], refs=[a.ref, b.ref]))
        self.stats["symbol_overlaps"] = n

    def on_sheet(self):
        self.checked.append("off_sheet")
        W, H = self.s.paper
        inner = (BORDER, BORDER, W - BORDER, H - BORDER)
        tb = (W - BORDER - TITLE_BLOCK[0], H - BORDER - TITLE_BLOCK[1], W - BORDER, H - BORDER)
        off, under = [], []
        items = [(p.ref, p.body) for p in self.s.parts if p.body] + [(t.owner + ": " + t.text, t.box) for t in self.s.texts]
        items += [("wire", (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))) for a, b in self.s.wires]
        for name, box in items:
            if box[0] < inner[0] or box[1] < inner[1] or box[2] > inner[2] or box[3] > inner[3]:
                off.append((name, box))
            elif _overlap(box, tb, 0.0):
                under.append((name, box))
        if off:
            self.findings.append(_f("off_sheet", "problem", f"{len(off)} item(s) outside the drawing area, e.g. {off[0][0]}",
                                    "They are cut off when the sheet is printed or exported, and easy to miss on screen. Move "
                                    "them in, or use a larger sheet or a hierarchical sheet.", "readability",
                                    off[0][1][0], off[0][1][1], examples=[o[0] for o in off[:10]]))
        if under:
            self.findings.append(_f("title_block", "warning", f"{len(under)} item(s) under the title block, e.g. {under[0][0]}",
                                    f"The title block (about {TITLE_BLOCK[0]:g} × {TITLE_BLOCK[1]:g} mm at the bottom right, "
                                    "approximate) is drawn over them.", "readability", under[0][1][0], under[0][1][1],
                                    examples=[u[0] for u in under[:10]]))
        xs = [b[0] for _, b in items] + [b[2] for _, b in items]
        ys = [b[1] for _, b in items] + [b[3] for _, b in items]
        if xs:
            used = ((max(xs) - min(xs)) * (max(ys) - min(ys))) / ((inner[2] - inner[0]) * (inner[3] - inner[1]))
            self.stats["sheet_area_used"] = round(min(used, 9.99), 2)

    def junctions(self):
        self.checked.append("four_way_junction")
        ends = {}
        for a, b in self.s.wires:
            ends[_key(a)] = ends.get(_key(a), 0) + 1
            ends[_key(b)] = ends.get(_key(b), 0) + 1
        points = set(ends) | {_key(j) for j in self.s.junctions}
        n = 0
        for p in points:
            arms = ends.get(p, 0) + 2 * sum(1 for a, b in self.s.wires if _on(p, a, b) and _key(a) != p and _key(b) != p)
            if arms >= 4:
                n += 1
                self.findings.append(_f("four_way_junction", "warning", f"four wires meet at ({p[0]:g}, {p[1]:g})",
                                        "A four-way junction reads as a crossing if its dot is lost; offset one wire to make "
                                        "two T junctions.", "junctions", p[0], p[1]))
        self.stats["four_way_junctions"] = n

    def power_symbols(self):
        self.checked.append("power_direction")
        for p in self.s.parts:
            if not p.power or not p.body:
                continue
            gnd = "GND" in p.value.upper() or p.value.upper() in ("VSS", "0V", "EARTH")
            if p.value.upper().startswith("PWR_FLAG"):
                continue
            # power symbols are drawn with their pin at the origin: the body lies below it (ground) or above it (supply)
            below = p.body[3] > p.at[1] + 0.5 and p.body[1] >= p.at[1] - 0.5
            above = p.body[1] < p.at[1] - 0.5 and p.body[3] <= p.at[1] + 0.5
            if gnd and above:
                self.findings.append(_f("power_direction", "note", f"ground symbol {p.value} points up",
                                        "Ground at the bottom, supplies at the top lets the sheet read like the circuit's "
                                        "voltage: route the wire down to the ground symbol.", "kicad_ssg", *p.at))
            elif not gnd and below and p.value.startswith(("+", "V")):
                self.findings.append(_f("power_direction", "note", f"supply symbol {p.value} points down",
                                        "Supplies at the top, ground at the bottom: route the wire up to the supply symbol.",
                                        "kicad_ssg", *p.at))

    def label_use(self):
        """How much of the sheet is joined by labels instead of wires, measured on two-terminal parts."""
        self.checked.append("label_use")
        wires = self.s.wires
        labels = {_key((x, y)): n for n, x, y in self.s.labels}
        pins_at = {}
        for p in self.s.parts:
            for num, x, y in p.pins:
                pins_at.setdefault(_key((x, y)), []).append(p.ref)

        def follow(pt):
            """Where a pin's wiring leads: 'label' if it is only a short stub to a label, 'wire' otherwise."""
            touching = [w for w in wires if _key(w[0]) == pt or _key(w[1]) == pt or _on(pt, *w)]
            if not touching:
                if pt in labels:
                    return "label"
                return "power" if len(pins_at.get(pt, [])) > 1 else "none"
            if len(touching) == 1:
                a, b = touching[0]
                other = _key(b) if _key(a) == pt else _key(a)
                length = math.dist(a, b)
                if other in labels and length <= STUB_MAX and len([w for w in wires if _key(w[0]) == other or _key(w[1]) == other]) == 1:
                    return "label"
                if len(pins_at.get(other, [])) and not any(r.startswith("#") for r in pins_at[other]):
                    return "wire"
                if any(r.startswith("#") for r in pins_at.get(other, [])):
                    return "power"
            return "wire"

        passive = 0
        by_label = []
        n_label_pins = n_wire_pins = 0
        for p in self.s.parts:
            if p.power or not p.pins:
                continue
            kinds = [follow(_key((x, y))) for _, x, y in p.pins]
            n_label_pins += kinds.count("label")
            n_wire_pins += kinds.count("wire")
            prefix = "".join(c for c in p.ref if c.isalpha())
            if len(p.pins) == 2 and prefix in _TWO_TERMINAL:
                passive += 1
                if "wire" not in kinds and "label" in kinds:
                    by_label.append(p.ref)
        self.stats.update({"labels": len(self.s.labels), "wire_segments": len(wires), "pins_by_wire": n_wire_pins,
                           "pins_by_label": n_label_pins, "two_terminal_parts": passive,
                           "two_terminal_by_label_only": len(by_label)})
        if passive and len(by_label) / passive > self.lim["passive_label_share"]:
            self.findings.append(_f("label_use", "warning",
                                    f"{len(by_label)} of {passive} resistors, capacitors and other two-terminal parts connect "
                                    "only through labels", "Within a functional block (a regulator and its caps, a crystal and "
                                    "its load caps, a pull-up and its pin) wires show at a glance what belongs together; "
                                    "labels make the reader search the sheet. Keep labels for nets that leave the block.",
                                    "heuristic", refs=by_label[:30], share=len(by_label) / passive,
                                    limit=self.lim["passive_label_share"]))
        elif by_label:
            self.findings.append(_f("label_use", "note", f"{len(by_label)} two-terminal part(s) connect only through labels",
                                    "Fine where the part belongs to another block or the wires would cross the sheet; "
                                    "otherwise draw the wire.", "heuristic", refs=by_label[:30]))


def run_erc(sch: Path) -> dict | None:
    cli = apps.find_kicad_cli()
    if not cli:
        return None
    out = Path(tempfile.mkdtemp(prefix="cadmate-erc-")) / "erc.json"
    try:
        proc = subprocess.run([cli, "sch", "erc", "--format", "json", "--severity-all", "--units", "mm", "--output", str(out), str(sch)],
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"kicad-cli erc failed: {e}"}
    if not out.exists():
        return {"error": f"kicad-cli erc wrote no report (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-800:]}"}
    return json.loads(out.read_text())


def _erc_findings(rep: dict) -> list[dict]:
    kinds: dict[tuple, list] = {}
    for sheet in rep.get("sheets", []):
        for v in sheet.get("violations", []):
            kinds.setdefault((v.get("type", "?"), v.get("severity", "?")), []).append(v)
    out = []
    for (kind, sev), vs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        its = vs[0].get("items", [])
        pos = its[0].get("pos", {}) if its else {}
        out.append(_f("erc", "problem" if sev == "error" else "note", f"ERC {sev}: {len(vs)} × {kind}: {vs[0].get('description', '')}",
                      "From KiCad's own checker, with the project's rules.", "kicad_erc",
                      pos.get("x"), pos.get("y"), example="; ".join(i.get("description", "") for i in its)[:200]))
    return out


_ORDER = {"problem": 0, "warning": 1, "note": 2}


def review_sheet(sheet: Sheet, limits: dict | None = None, erc: dict | None = None) -> dict:
    lim = {**LIMITS, **(limits or {})}
    r = Review(sheet, lim)
    r.text_overlaps()
    r.body_overlaps()
    r.on_sheet()
    r.junctions()
    r.power_symbols()
    r.label_use()
    findings = r.findings
    not_checked = []
    if erc is not None and "error" not in erc:
        findings = _erc_findings(erc) + findings
        r.checked.append("kicad_erc")
    elif erc is not None:
        not_checked.append({"rule": "kicad_erc", "why": erc["error"]})
    else:
        not_checked.append({"rule": "kicad_erc", "why": f"kicad-cli not found, so connectivity wasn't checked; set {apps.KICAD_CLI_ENV}"})
    findings.sort(key=lambda f: _ORDER[f["severity"]])
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in _ORDER}
    used = sorted({f["source"] for f in findings})
    return {"ok": True, "sheet": sheet.name, "paper_mm": list(sheet.paper), "counts": counts, "findings": findings[:LISTED],
            "more": max(0, len(findings) - LISTED), "stats": r.stats, "checked": r.checked, "not_checked": not_checked,
            "limits": lim, "sources": {k: SOURCES[k] for k in used},
            "note": "Text widths are estimated from the font size (KiCad's stroke font, about ±15%); check flagged spots by eye.",
            "units": "mm, KiCad's schematic frame (Y down)"}


def review_file(path: str, limits: dict | None = None, erc: bool = True) -> dict:
    f = Path(path).expanduser()
    sheet = read(f)
    return review_sheet(sheet, limits, run_erc(f) if erc else None)
