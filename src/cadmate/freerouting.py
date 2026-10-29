"""Let Freerouting route what the critical pass left, and bring its result back into KiCad.

The round trip: a copy of the board goes out as a Specctra DSN (KiCad's own exporter, run in KiCad's bundled Python,
since kicad-cli has no DSN step), with the widths the high-current items need written into the DSN as net classes.
Freerouting runs headless on it. Its session file (SES) is read here rather than by KiCad's importer, so the result can
go into the board open in the editor as one undo step, without the user reloading anything. On the way in, tracks
narrower than their net class (Freerouting 2.5's fan-out puts down tracks below the board minimum) are widened to it
where clearance allows, and listed; where it doesn't, they stay as wide as they can and are reported. Locked tracks go out as protected wiring, so Freerouting routes around them and leaves them alone.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import sexpr
from .pcb_board import Board, NetGraph, Track, Via, dist, parse

JAR_ENV = "CADMATE_FREEROUTING_JAR"
JAVA_ENV = "CADMATE_JAVA"
KICAD_PYTHON_ENV = "CADMATE_KICAD_PYTHON"

_JARS = {
    "Darwin": ["~/Documents/KiCad/*/3rdparty/plugins/*freerouting*/jar/*.jar", "~/Library/Application Support/kicad/*/3rdparty/plugins/*freerouting*/jar/*.jar"],
    "Linux": ["~/.local/share/kicad/*/3rdparty/plugins/*freerouting*/jar/*.jar", "~/Documents/KiCad/*/3rdparty/plugins/*freerouting*/jar/*.jar"],
    "Windows": ["~/Documents/KiCad/*/3rdparty/plugins/*freerouting*/jar/*.jar"],
}
_JAVAS = {"Darwin": ["/opt/homebrew/opt/openjdk/bin/java", "/usr/local/opt/openjdk/bin/java"], "Linux": [], "Windows": []}
_KICAD_PYTHONS = {
    "Darwin": ["/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/*/bin/python3"],
    "Linux": ["/usr/bin/python3"],
    "Windows": [r"C:\Program Files\KiCad\*\bin\python.exe"],
}


class RouteError(RuntimeError):
    pass


def _version_key(path: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", Path(path).name)
    return tuple(int(x) for x in m.groups()) if m else (0,)


def find_jar() -> str | None:
    if os.environ.get(JAR_ENV):
        return os.environ[JAR_ENV] if Path(os.environ[JAR_ENV]).exists() else None
    hits = [h for pat in _JARS.get(platform.system(), []) for h in glob.glob(os.path.expanduser(pat))]
    return max(hits, key=_version_key) if hits else None


def find_java() -> str | None:
    if os.environ.get(JAVA_ENV):
        return os.environ[JAVA_ENV]
    if os.environ.get("JAVA_HOME") and Path(os.environ["JAVA_HOME"], "bin", "java").exists():
        return str(Path(os.environ["JAVA_HOME"], "bin", "java"))
    for c in _JAVAS.get(platform.system(), []):
        if Path(c).exists():
            return c
    return shutil.which("java")


def find_kicad_python() -> str | None:
    if os.environ.get(KICAD_PYTHON_ENV):
        return os.environ[KICAD_PYTHON_ENV]
    for pat in _KICAD_PYTHONS.get(platform.system(), []):
        for hit in sorted(glob.glob(pat), reverse=True):
            try:
                if subprocess.run([hit, "-c", "import pcbnew"], capture_output=True, timeout=60).returncode == 0:
                    return hit
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def status() -> dict:
    jar, java, py = find_jar(), find_java(), find_kicad_python()
    return {"freerouting_jar": jar, "java": java, "kicad_python": py, "ready": bool(jar and java and py),
            "set": {JAR_ENV: "the Freerouting .jar", JAVA_ENV: "java", KICAD_PYTHON_ENV: "the Python that ships with KiCad (has pcbnew)"}}


def _kicad_py(script: str, *args: str, timeout: int = 300) -> str:
    """Run a pcbnew script in KiCad's Python. One board per process: pcbnew's LoadBoard can't run twice in one."""
    py = find_kicad_python()
    if not py:
        raise RouteError(f"KiCad's Python (with pcbnew) not found; set {KICAD_PYTHON_ENV}")
    proc = subprocess.run([py, "-c", script, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RouteError(f"KiCad's Python failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}")
    return proc.stdout


def export_dsn(board_file: Path, dsn: Path) -> None:
    _kicad_py("import sys, pcbnew\nb = pcbnew.LoadBoard(sys.argv[1])\nok = pcbnew.ExportSpecctraDSN(b, sys.argv[2])\n"
              "sys.exit(0 if ok else 3)", str(board_file), str(dsn))
    if not dsn.exists():
        raise RouteError("KiCad wrote no DSN")


def refill(board_file: Path) -> None:
    _kicad_py("import sys, pcbnew\nb = pcbnew.LoadBoard(sys.argv[1])\npcbnew.ZONE_FILLER(b).Fill(b.Zones())\npcbnew.SaveBoard(sys.argv[1], b)",
              str(board_file))


# --- DSN: widths for the autorouter ------------------------------------------------------------------------


def _q(name: str) -> str:
    """A net name as a DSN token: bare when it is plain letters and digits, quoted otherwise (as KiCad writes them)."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else '"' + name.replace('"', '\\"') + '"'


def dsn_scale(text: str) -> float:
    """DSN units per mm, from the structure's (unit ...) or the resolution."""
    m = re.search(r"\(unit\s+(\w+)\)", text) or re.search(r"\(resolution\s+(\w+)\s+[\d.]+\)", text)
    unit = m.group(1).lower() if m else "um"
    return {"um": 1000.0, "mm": 1.0, "mil": 1 / 0.0254, "inch": 1 / 25.4, "cm": 0.1}.get(unit, 1000.0)


def dsn_widths(text: str, widths: dict[str, float]) -> str:
    """Put each net with a width into its own class carrying that width, taking it out of the class it was in."""
    if not widths:
        return text
    k = dsn_scale(text)
    for net in widths:
        for token in {_q(net), '"' + net + '"', net}:
            text = re.sub(r"(\(class\s+[^()]*?)(?<=\s)" + re.escape(token) + r"(?=[\s)])", r"\1", text)
    first = text[text.find("(class "):] if "(class " in text else ""
    clearance = re.search(r"\(clearance\s+([\d.]+)\)", first)
    use_via = re.search(r"\(use_via\s+(\"[^\"]*\"|[^\s()]+)\s*\)", first)
    classes = []
    for i, (net, w) in enumerate(sorted(widths.items())):
        rule = f"(width {w * k:.1f})" + (f" (clearance {clearance.group(1)})" if clearance else "")
        circuit = f"\n      (circuit (use_via {use_via.group(1)}))" if use_via else ""
        classes.append(f"    (class cadmate_wide_{i} {_q(net)}{circuit}\n      (rule {rule})\n    )\n")
    idx = _section_end(text, "(network")
    if idx is None:
        raise RouteError("couldn't find the DSN's network section")
    return text[:idx] + "".join(classes) + text[idx:]


def dsn_keepouts(text: str, board: Board) -> str:
    """Solder mask openings (logos, test areas) as keepouts: KiCad's exporter leaves them out, so Freerouting would run
    tracks through them, bare under the opening."""
    if not board.mask_openings:
        return text
    start, end = text.find("(placement"), _section_end(text, "(placement")
    # only the placement is parsed: the header's (string_quote ") isn't valid s-expression
    root = sexpr.parse("(pcb " + text[start:end + 1] + ")") if start != -1 and end else sexpr.parse("(pcb)")
    to_board, scale = _transform(root, board, dsn_scale(text))
    ox, oy = to_board(0, 0)
    out = []
    for layer, poly in board.mask_openings:
        pts = " ".join(f"{(x - ox) * scale:.1f} {-(y - oy) * scale:.1f}" for x, y in poly + poly[:1])
        out.append(f"    (keepout \"\" (polygon {layer} 0 {pts}))\n")
    idx = _section_end(text, "(structure")
    if idx is None:
        raise RouteError("couldn't find the DSN's structure section")
    return text[:idx] + "".join(out) + text[idx:]


def _section_end(text: str, head: str) -> int | None:
    """Index of the closing paren of the first section starting with head, skipping quoted strings."""
    start = text.find(head)
    if start == -1:
        return None
    depth, i, quoted = 0, start, False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


# --- Freerouting ---------------------------------------------------------------------------------------


def run_freerouting(dsn: Path, ses: Path, passes: int = 30, timeout: int = 1200) -> dict:
    jar, java = find_jar(), find_java()
    if not jar:
        raise RouteError(f"Freerouting's jar not found; set {JAR_ENV} (KiCad's Freerouting plugin keeps it under 3rdparty/plugins/…/jar)")
    if not java:
        raise RouteError(f"java not found; set {JAVA_ENV}")
    cmd = [java, "-jar", jar, "-de", str(dsn), "-do", str(ses), "-mp", str(passes), "--gui.enabled=false"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RouteError(f"Freerouting didn't finish in {timeout} s") from e
    log = (proc.stdout or "") + (proc.stderr or "")
    if not ses.exists():
        raise RouteError(f"Freerouting wrote no session (exit {proc.returncode}): {log[-2000:]}")
    unrouted = re.findall(r"(\d+)\s+unrouted", log, re.I)
    return {"jar": Path(jar).name, "exit": proc.returncode, "unrouted_reported": int(unrouted[-1]) if unrouted else None,
            "errors": sorted(set(re.findall(r"(?:Exception|ERROR)[^\n]{0,160}", log)))[:8], "log_tail": log[-1500:]}


# --- SES: what Freerouting routed ------------------------------------------------------------------------


def _res(node: sexpr.Node | None) -> float:
    """Counts per mm from a (resolution unit n) node."""
    if node is None:
        return 10000.0
    a = node.atoms()
    unit = {"um": 1000.0, "mm": 1.0, "mil": 1 / 0.0254, "inch": 1 / 25.4}.get((a[0] if a else "um").lower(), 1000.0)
    return unit * (float(a[1]) if len(a) > 1 else 1.0)


def _transform(root: sexpr.Node, board: Board, per_mm: float):
    """SES coordinates to KiCad's: scale, flip Y, and an offset found from the components' placements where the SES lists them."""
    places = []
    pl = root.child("placement")
    if pl is not None:
        for comp in pl.children("component"):
            for p in comp.children("place"):
                a = p.atoms()
                if len(a) >= 3:
                    places.append((a[0], float(a[1]), float(a[2])))
    pos = {p.ref: (p.x, p.y) for p in board.parts}
    best = None
    for scale in {per_mm, 1000.0, 10000.0, 100000.0}:
        dxs = sorted(pos[r][0] - x / scale for r, x, y in places if r in pos)
        dys = sorted(pos[r][1] + y / scale for r, x, y in places if r in pos)
        if not dxs:
            continue
        ox, oy = dxs[len(dxs) // 2], dys[len(dys) // 2]
        err = sum(abs(pos[r][0] - (x / scale + ox)) + abs(pos[r][1] - (-y / scale + oy)) for r, x, y in places if r in pos) / len(dxs)
        if best is None or err < best[0]:
            best = (err, scale, ox, oy)
    if best is None or best[0] > 0.05:
        best = (0.0, per_mm, 0.0, 0.0)
    _, scale, ox, oy = best
    return lambda x, y: (round(x / scale + ox, 4), round(-y / scale + oy, 4)), scale


def parse_ses(text: str, board: Board, via_default: tuple[float, float] = (0.6, 0.3)) -> tuple[list[Track], list[Via]]:
    root = sexpr.parse(text)
    routes = root.child("routes")
    if routes is None:
        raise RouteError("the session file has no routes")
    per_mm = _res(routes.child("resolution"))
    xf, scale = _transform(root, board, per_mm)
    stacks: dict[str, tuple[float, float]] = {}
    lib = routes.child("library_out")
    if lib is not None:
        for ps in lib.children("padstack"):
            name = (ps.atoms() or [""])[0]
            m = re.search(r"_(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)_um", name)
            if m:
                stacks[name] = (float(m.group(1)) / 1000, float(m.group(2)) / 1000)
            else:
                d = [s.child("circle").floats() for s in ps.children("shape") if s.child("circle") is not None]
                if d and d[0]:
                    stacks[name] = (d[0][0] / scale, via_default[1])
    tracks, vias = [], []
    out = routes.child("network_out")
    for net in (out.children("net") if out is not None else []):
        name = (net.atoms() or [""])[0]
        for w in net.children("wire"):
            path = w.child("path")
            if path is None:
                continue
            a = path.atoms()
            layer, nums = a[0], [float(x) for x in a[1:]]
            width = nums[0] / scale
            pts = [xf(nums[i], nums[i + 1]) for i in range(1, len(nums) - 1, 2)]
            for p, q in zip(pts, pts[1:]):
                if dist(p, q) > 1e-6:
                    tracks.append(Track(p, q, round(width, 4), layer, name))
        for v in net.children("via"):
            a = v.atoms()
            if len(a) >= 3:
                size, drill = stacks.get(a[0], via_default)
                x, y = xf(float(a[1]), float(a[2]))
                vias.append(Via(x, y, size, drill, [board.copper_layers[0], board.copper_layers[-1]] if board.copper_layers else ["F.Cu", "B.Cu"], name))
    return tracks, vias


def new_items(board: Board, tracks: list[Track], vias: list[Via]) -> tuple[list[Track], list[Via]]:
    """The SES lists the protected wiring too; keep only what isn't on the board already."""

    def tkey(t: Track):
        a, b = sorted([(round(t.start[0], 3), round(t.start[1], 3)), (round(t.end[0], 3), round(t.end[1], 3))])
        return (t.net, t.layer, a, b)

    have = {tkey(t) for t in board.tracks}
    vhave = {(v.net, round(v.x, 3), round(v.y, 3)) for v in board.vias}
    return ([t for t in tracks if tkey(t) not in have], [v for v in vias if (v.net, round(v.x, 3), round(v.y, 3)) not in vhave])


def widen(board: Board, tracks: list[Track], widths: dict[str, float], vias: list[Via] = ()) -> list[dict]:
    """Bring tracks up to their net's width (the net class's, the board minimum, or the width asked for in widths) where
    that keeps clearance. Freerouting necks tracks down between fine-pitch pins, sometimes below the board minimum; a
    segment that can't be widened without breaking clearance is taken as wide as it can go and reported. Returns what
    changed, and what is still narrower than wanted."""
    import copy

    from .pcb_route import Router

    rules = board.rules or {}
    floor = rules.get("min_track_width") or 0.0
    todo = []
    for t in tracks:
        nc = board.net_class(t.net)
        want = max(widths.get(t.net, 0), (nc.track_width if nc and nc.track_width else 0) or 0, floor)
        if t.width < want - 1e-4:
            todo.append((t, want))
    if not todo:
        return []
    whole = copy.copy(board)
    whole.tracks, whole.vias = board.tracks + tracks, board.vias + list(vias)
    r = Router(whole)

    def blocked(t: Track, w: float, ends: tuple) -> str | None:
        for a, b in t.pieces():
            ok, what = r._segment_clear(t.layer, a, b, t.net, w / 2, ends)
            if not ok:
                return what or "clearance"
        return None

    changed: dict[tuple, dict] = {}
    for t, want in todo:
        ends = tuple(q for q in whole.pads if q.net == t.net and any(q.contains(e) for e in (t.start, t.end)))
        w, what = want, None
        while w > t.width + 1e-4:
            hit = blocked(t, w, ends)
            if hit is None:
                break
            what = what or hit
            w = round(w - 0.005, 4)
        w = max(w, t.width)
        k = (t.net, t.width, want, round(w, 4))
        e = changed.setdefault(k, {"net": t.net, "from_mm": t.width, "to_mm": round(w, 4), "segments": 0})
        e["segments"] += 1
        if w < want - 1e-4:
            e |= {"wanted_mm": want, "held_back_by": what, "at": [round(t.start[0], 2), round(t.start[1], 2)]}
            if w < floor - 1e-4:
                e["below_board_minimum"] = floor
        if w > t.width + 1e-4:
            t.width = w
            r.obs.add_track(t)
    return list(changed.values())


def fab_check(board: Board, tracks: list[Track], lim: dict) -> dict:
    """Sort the new tracks narrower than the fab's preferred width: below its hard minimum is an error, between the two a
    warning. A neck is the short end of a track going into a pad; anything else narrow was squeezed between other copper."""
    rule = lim["rules"]["track"]
    hard, pref = rule["hard"], rule["preferred"]
    out: dict[str, list] = {"errors": [], "warnings": []}
    for t in tracks:
        if t.width >= pref - 1e-6:
            continue
        pad = next((q for q in board.pads if q.net == t.net and (q.contains(t.start) or q.contains(t.end))), None)
        e = {"net": t.net, "width_mm": round(t.width, 4), "length_mm": round(t.length, 2), "layer": t.layer,
             "at": [round(t.start[0], 2), round(t.start[1], 2)],
             "what": f"neck into {pad.name}" if pad and t.length <= 1.0 else "narrow between other copper"}
        if t.width < hard - 1e-6:
            out["errors"].append(e | {"why": f"under {lim['fab']}'s {hard:g} mm minimum: reroute it, or route it by hand"})
        else:
            out["warnings"].append(e | {"why": f"{lim['fab']} can make it, but it's under {pref:g} mm, their minimum plus "
                                               f"{lim['margin']:.0%}: keep it only where there's no room"})
    return out


def unrouted(board: Board) -> list[dict]:
    """Nets whose pads aren't all joined by copper (pours as filled now), biggest first."""
    out = []
    for net in board.nets:
        pads = [p for p in board.pads if p.net == net]
        if len(pads) < 2:
            continue
        isl = NetGraph(board, net).islands()
        if len(isl) > 1:
            out.append({"net": net, "islands": len(isl), "pads_apart": sorted({p.name for g in sorted(isl, key=len)[:-1] for p in g})[:6]})
    return sorted(out, key=lambda e: -e["islands"])


# --- the round trip --------------------------------------------------------------------------------------


def autoroute_file(board_file: Path, out_file: Path, widths: dict[str, float] | None = None, passes: int = 30, timeout: int = 1200,
                   fab_limits: dict | None = None) -> dict:
    from . import pcb_route

    widths = widths or {}
    work = Path(tempfile.mkdtemp(prefix="cadmate-fr-"))
    dsn, ses = work / (board_file.stem + ".dsn"), work / (board_file.stem + ".ses")
    board = parse(board_file.read_text(), board_file.name)
    from .pcb_board import load_project

    load_project(board, board_file.with_suffix(".kicad_pro"))
    export_dsn(board_file, dsn)
    dsn.write_text(dsn_keepouts(dsn_widths(dsn.read_text(), widths), board))
    fr = run_freerouting(dsn, ses, passes, timeout)
    tracks, vias = parse_ses(ses.read_text(), board)
    tracks, vias = new_items(board, tracks, vias)
    widened = widen(board, tracks, widths, vias)
    pcb_route.write_file(board_file, out_file, tracks, vias, locked=False)
    filled = True
    try:
        refill(out_file)
    except RouteError as e:
        filled = str(e)
    after = parse(out_file.read_text(), out_file.name)
    load_project(after, out_file.with_suffix(".kicad_pro"))
    out = {"ok": True, "board": str(out_file), "freerouting": fr, "added": {"tracks": len(tracks), "vias": len(vias)},
           "widened": widened, "zones_refilled": filled, "unrouted": unrouted(after), "work": str(work)}
    if fab_limits:
        out["fab_check"] = fab_check(board, tracks, fab_limits)
    return out


def autoroute_live(widths: dict[str, float] | None = None, passes: int = 30, timeout: int = 1200, fab_limits: dict | None = None) -> dict:
    from . import kicad_pcb_live, pcb_route
    from .pcb_board import load_project

    widths = widths or {}
    kicad, live = kicad_pcb_live.connect()
    work = Path(tempfile.mkdtemp(prefix="cadmate-fr-"))
    copy = work / Path(live.name).name
    live.save_as(str(copy), overwrite=True, include_project=True)
    dsn, ses = copy.with_suffix(".dsn"), copy.with_suffix(".ses")
    board = parse(copy.read_text(), copy.name)
    load_project(board, copy.with_suffix(".kicad_pro"))
    export_dsn(copy, dsn)
    dsn.write_text(dsn_keepouts(dsn_widths(dsn.read_text(), widths), board))
    fr = run_freerouting(dsn, ses, passes, timeout)
    tracks, vias = parse_ses(ses.read_text(), board)
    tracks, vias = new_items(board, tracks, vias)
    widened = widen(board, tracks, widths, vias)

    res = kicad_pcb_live.commit(f"Freerouting: {len(tracks)} tracks, {len(vias)} vias",
                                lambda kicad, b: pcb_route.apply_live(b, tracks, vias, locked=False))
    if not res.get("ok"):
        return res | {"freerouting": fr}
    refill_note = "done, as its own undo step"
    r2 = kicad_pcb_live.commit("refill zones", lambda kicad, b: b.refill_zones())
    if not r2.get("ok"):
        refill_note = f"failed ({r2.get('error')}); press B in the PCB editor to refill"
    _, live = kicad_pcb_live.connect()
    after = parse(live.get_as_string(), live.name)
    load_project(after, copy.with_suffix(".kicad_pro"))
    out = {"ok": True, "board": live.name, "freerouting": fr, "added": {"tracks": len(tracks), "vias": len(vias)}, "widened": widened,
           "zones": refill_note, "unrouted": unrouted(after),
           "undo": "two steps in KiCad's undo list: 'cadmate: Freerouting …' and 'cadmate: refill zones'", "work": str(work)}
    if fab_limits:
        out["fab_check"] = fab_check(board, tracks, fab_limits)
    return out
