"""Manufacturing files for a finished board: Gerbers and drill files zipped for upload, a BOM and a placement file.

Gerbers, drill and positions come from kicad-cli, so they are exactly what KiCad's plot dialog would give. The BOM is
read from the board's footprints (value, footprint, and the part-number fields the schematic gave them); parts marked
DNP or excluded from the BOM are left out, and parts with no part number are listed so the order isn't short.

An assembly drawing goes with them for any house: one PDF page per side that has parts, from the fabrication layers
(part outlines and references) over the board outline, the bottom one mirrored as seen from below.

No board house is assumed. The generic BOM (manufacturer and part number first, any distributor numbers beside them)
and placement file go to any assembler. When the board house is one cadmate knows the columns for (JLCPCB), its own BOM
and CPL are written as well.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import sexpr, versions
from .apps import KICAD_CLI_ENV, find_kicad_cli

GERBER_LAYERS = ["F.Cu", "B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "Edge.Cuts"]
INNER = ["In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu"]


class ExportError(RuntimeError):
    pass


def _cli(*args: str, timeout: int = 300) -> str:
    cli = find_kicad_cli()
    if not cli:
        raise ExportError(f"kicad-cli not found; set {KICAD_CLI_ENV}")
    try:
        r = subprocess.run([cli, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ExportError(f"kicad-cli {args[0]} {args[1]} didn't finish in {timeout // 60} min; try again, or run it in "
                          "KiCad to see where it stops") from None
    if r.returncode != 0:
        raise ExportError(f"kicad-cli {args[0]} {args[1]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return "\n".join(x for x in (r.stdout, r.stderr) if x)  # what it printed, warnings included


def _prop(fp: sexpr.Node, name: str) -> str:
    for p in fp.children("property"):
        a = p.atoms()
        if a and a[0] == name:
            return a[1] if len(a) > 1 else ""
    return ""


# part-number fields as schematics name them, matched without case, spaces, dots, dashes or underscores
FIELDS = {
    "manufacturer": ("manufacturer", "mfr", "mfg", "mf", "manufacturername"),
    "mpn": ("mpn", "manufacturerpartnumber", "mfrpartnumber", "mfrno", "mfgpartnumber", "partnumber", "pn",
            "manufacturerpn"),
    "lcsc": ("lcsc", "lcscpart", "lcscpartnumber", "jlcpcbpart", "jlcpcbpartnumber", "jlcpcb"),
    "digikey": ("digikey", "digikeypartnumber", "digikeypn", "digikeypart"),
    "mouser": ("mouser", "mouserpartnumber", "mouserpn", "mouserpart"),
    "package": ("package", "case", "packagecase", "casepackage", "pkg"),  # as the distributor lists it, e.g. SOT-23-5
}
DISTRIBUTORS = ("lcsc", "digikey", "mouser")


def _norm(name: str) -> str:
    return re.sub(r"[\s._#-]", "", name.lower())


def _fields(fp: sexpr.Node) -> dict[str, str]:
    props = {}
    for p in fp.children("property"):
        a = p.atoms()
        if a:
            props.setdefault(_norm(a[0]), a[1] if len(a) > 1 else "")
    return {k: next((props[n] for n in names if props.get(n, "").strip()), "") for k, names in FIELDS.items()}


def footprints(text: str) -> list[dict]:
    """Each footprint's reference, value, footprint name, part numbers (manufacturer, MPN, LCSC, DigiKey, Mouser), the
    package as the distributor lists it, side, and whether it goes in the BOM / CPL."""
    root = sexpr.parse(text)
    out = []
    for fp in root.children("footprint"):
        lib = (fp.atoms() or [""])[0]
        attr = fp.child("attr")
        flags = set(attr.atoms()) if attr is not None else set()
        dnp = "dnp" in flags or (fp.child("dnp") is not None and "yes" in fp.child("dnp").atoms())
        layer = (fp.child("layer").atoms() or ["F.Cu"])[0] if fp.child("layer") is not None else "F.Cu"
        out.append({"ref": _prop(fp, "Reference"), "value": _prop(fp, "Value"), "footprint": lib.split(":")[-1], **_fields(fp),
                    "side": "bottom" if layer == "B.Cu" else "top", "smd": "smd" in flags,
                    "bom": not dnp and "exclude_from_bom" not in flags and "board_only" not in flags,
                    "pos": not dnp and "exclude_from_pos_files" not in flags and "board_only" not in flags})
    return out


def _refkey(r: str):
    head = r.rstrip("0123456789")
    tail = r[len(head):]
    return (head, int(tail) if tail else 0)


def _in_bom(p: dict) -> bool:
    return p["bom"] and p["ref"] and not p["ref"].startswith("#")


def generic_bom(parts: list[dict]) -> str:
    """A BOM any assembler or buyer can read: one line per value + footprint + part number, with quantity, manufacturer
    and part number, and the distributor numbers the schematic has (a column only when some part has one)."""
    groups: dict[tuple, list[str]] = {}
    for p in parts:
        if _in_bom(p):
            groups.setdefault((p["value"], p["footprint"], p["manufacturer"], p["mpn"], *(p[d] for d in DISTRIBUTORS)), []).append(p["ref"])
    dists = [d for i, d in enumerate(DISTRIBUTORS) if any(k[4 + i] for k in groups)]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Designator", "Quantity", "Value", "Footprint", "Manufacturer", "MPN"] +
               [{"lcsc": "LCSC", "digikey": "DigiKey", "mouser": "Mouser"}[d] for d in dists])
    for key, refs in sorted(groups.items(), key=lambda e: _refkey(min(e[1], key=_refkey))):
        value, fp, mfr, mpn, *dist = key
        w.writerow([",".join(sorted(refs, key=_refkey)), len(refs), value, fp, mfr, mpn] +
                   [dist[DISTRIBUTORS.index(d)] for d in dists])
    return buf.getvalue()


def generic_pos(kicad_pos_csv: str, parts: list[dict]) -> str:
    """kicad-cli's position CSV with only the parts that get placed: Designator, Value, Footprint, X and Y in mm,
    Rotation, Side. KiCad's board frame: Y down, origin at the board's drill/place origin if set."""
    keep = {p["ref"] for p in parts if p["bom"] and p["pos"]}
    rows = list(csv.DictReader(io.StringIO(kicad_pos_csv)))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Designator", "Value", "Footprint", "X (mm)", "Y (mm)", "Rotation", "Side"])
    for r in sorted(rows, key=lambda r: _refkey(r["Ref"])):
        if r["Ref"] in keep:
            w.writerow([r["Ref"], r["Val"], r["Package"], f"{float(r['PosX']):.4f}", f"{float(r['PosY']):.4f}",
                        f"{float(r['Rot']) % 360:g}", "bottom" if r["Side"].lower().startswith("b") else "top"])
    return buf.getvalue()


def jlc_bom(parts: list[dict]) -> str:
    """JLCPCB's BOM: Comment, Designator, Footprint, LCSC Part #, one line per value + footprint + part number."""
    groups: dict[tuple, list[str]] = {}
    for p in parts:
        if _in_bom(p):
            groups.setdefault((p["value"], p["footprint"], p["lcsc"]), []).append(p["ref"])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for (value, fp, lcsc), refs in sorted(groups.items(), key=lambda e: _refkey(min(e[1], key=_refkey))):
        w.writerow([value, ",".join(sorted(refs, key=_refkey)), fp, lcsc])
    return buf.getvalue()


def jlc_cpl(kicad_pos_csv: str, parts: list[dict]) -> str:
    """kicad-cli's position CSV (Ref, Val, Package, PosX, PosY, Rot, Side) as JLCPCB's CPL: Designator, Mid X, Mid Y,
    Layer, Rotation. Only parts that are in the BOM; JLC places nothing else."""
    keep = {p["ref"] for p in parts if p["bom"] and p["pos"]}
    rows = list(csv.DictReader(io.StringIO(kicad_pos_csv)))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for r in sorted(rows, key=lambda r: _refkey(r["Ref"])):
        if r["Ref"] not in keep:
            continue
        w.writerow([r["Ref"], f"{float(r['PosX']):.4f}mm", f"{float(r['PosY']):.4f}mm",
                    "Bottom" if r["Side"].lower().startswith("b") else "Top", f"{float(r['Rot']) % 360:g}"])
    return buf.getvalue()


FAB_LAYER = {"top": "F.Fab", "bottom": "B.Fab"}


def no_fab_drawing(text: str) -> list[str]:
    """Placed parts whose footprint draws nothing on its side's fabrication layer, so they'd be blank on the assembly
    drawing (a footprint's F.Fab outline and reference are what it shows; a bottom part's are on B.Fab)."""
    root = sexpr.parse(text)
    out = []
    placed = {p["ref"] for p in footprints(text) if p["bom"] and p["pos"]}
    for fp in root.children("footprint"):
        ref = _prop(fp, "Reference")
        if ref not in placed:
            continue
        layer = (fp.child("layer").atoms() or ["F.Cu"])[0] if fp.child("layer") is not None else "F.Cu"
        fab = FAB_LAYER["bottom" if layer == "B.Cu" else "top"]
        drawn = any(isinstance(c, sexpr.Node) and c.child("layer") is not None and fab in c.child("layer").atoms()
                    and not _hidden_text(c) for c in fp.items[1:])
        if not drawn:
            out.append(ref)
    return sorted(out, key=_refkey)


def _hidden_text(node: sexpr.Node) -> bool:
    """A footprint text or property that isn't plotted: (hide yes), the older bare hide, or hidden in its effects."""
    holders = [node] + [e for e in node.children("effects")]
    for h in holders:
        hide = h.child("hide")
        if "hide" in h.atoms() or (hide is not None and "no" not in hide.atoms()):
            return True
    return False


def assembly_drawing(board: Path, out_dir: Path, sides: list[str] | None = None) -> dict:
    """The assembly drawing: <board>-assembly-top.pdf and, when parts sit underneath, <board>-assembly-bottom.pdf
    (mirrored, as the assembler sees that side). Each is the side's fabrication layer over the board outline: part
    outlines with their references; DNP parts are crossed out. sides: which to draw, default those with placed parts."""
    text = board.read_text(encoding="utf-8", errors="replace")
    if sides is None:
        sides = sorted({p["side"] for p in footprints(text) if p["bom"] and p["pos"]}, reverse=True)
    out = {}
    for side in sides:
        pdf = out_dir / f"{board.stem}-assembly-{side}.pdf"
        args = versions.kicad_cli_args("pcb_pdf") + (["--mirror"] if side == "bottom" else [])
        _cli(*args, "--layers", f"{FAB_LAYER[side]},Edge.Cuts", "--output", str(pdf), str(board))
        out[side] = str(pdf)
    reply = {"assembly_drawing": out}
    if blank := no_fab_drawing(text):
        reply["not_on_assembly_drawing"] = blank
    return reply


def copper_layers(text: str) -> list[str]:
    root = sexpr.parse(text)
    layers = root.child("layers")
    names = [n.atoms()[0] for n in (layers.items[1:] if layers else []) if isinstance(n, sexpr.Node) and n.atoms()]
    return [l for l in ["F.Cu", *INNER, "B.Cu"] if l in names]


def placeholder(lcsc: str) -> bool:
    """A part number that isn't one yet: empty, TBD, or still carrying a question mark."""
    return not re.fullmatch(r"C\d+", lcsc.strip())


def export(board: Path, out_dir: Path | None = None, refill: bool = True, fab: str | None = None) -> dict:
    """Gerbers + drill (zipped), a generic BOM and placement file, and the board house's own BOM and CPL when cadmate
    knows its columns, into out_dir (default: <project>/fab/<board name> inside a cadmate project, else <board
    folder>/fab/<board name>). fab: the board house, else the project's choice, else none ("none" to skip it). The
    folder is regenerated: earlier Gerbers in it are removed first, so a layer the board no longer has can't ride along in
    the zip. With refill, the zones are refilled on a copy first: saved fills go stale after any edit, and the Gerbers
    are the fills."""
    from . import fab as fabmod
    from . import project

    board = board.expanduser()
    if not board.exists():
        raise ExportError(f"no board at {board}")
    root = project.find_root(board)
    chosen = fabmod.resolve(fab, board)
    source_sha = hashlib.sha256(board.read_bytes()).hexdigest()  # which state of the board these files are, for releases
    out = out_dir.expanduser() if out_dir else (root / "fab" / board.stem if root else board.parent / "fab" / board.stem)
    gerb = out / "gerbers"
    if gerb.exists():
        shutil.rmtree(gerb)
    gerb.mkdir(parents=True, exist_ok=True)
    if refill:
        from .freerouting import refill as _refill

        scratch = tempfile.TemporaryDirectory(prefix="cadmate-fab-")
        copy = Path(scratch.name) / board.name  # not in the fab folder: it isn't for the fab
        shutil.copyfile(board, copy)
        for ext in (".kicad_pro", ".kicad_dru"):
            if board.with_suffix(ext).exists():
                shutil.copyfile(board.with_suffix(ext), copy.with_suffix(ext))
        try:
            _refill(copy)
        except Exception as e:  # noqa: BLE001
            raise ExportError(f"couldn't refill the zones ({e}); refill in KiCad (B), save, and export with refill=False") from None
        board = copy
    text = board.read_text(encoding="utf-8", errors="replace")
    layers = [l for l in GERBER_LAYERS if l != "F.Cu" and l != "B.Cu"] + copper_layers(text)
    _cli(*versions.kicad_cli_args("pcb_gerbers"), "--output", str(gerb) + os.sep, "--layers", ",".join(layers), str(board))
    _cli(*versions.kicad_cli_args("pcb_drill"), "--output", str(gerb) + os.sep, str(board))
    zpath = out / f"{board.stem}-gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(gerb.iterdir()):
            z.write(f, f.name)
    stem = board.stem
    for old in (f"{stem}-pos-kicad.csv", f"{stem}-bom-jlc.csv", f"{stem}-cpl-jlc.csv", f"{stem}-assembly-top.pdf",
                f"{stem}-assembly-bottom.pdf"):  # a previous export's, maybe for another house
        (out / old).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="cadmate-pos-") as d:
        tmp = Path(d) / f"{stem}-pos.csv"
        _cli(*versions.kicad_cli_args("pcb_pos"), "--output", str(tmp), str(board))
        kicad_pos = tmp.read_text(encoding="utf-8")
    parts = footprints(text)
    bom, pos = out / f"{stem}-bom.csv", out / f"{stem}-pos.csv"
    bom.write_text(generic_bom(parts), encoding="utf-8")
    pos.write_text(generic_pos(kicad_pos, parts), encoding="utf-8")
    listed = [p for p in parts if _in_bom(p)]
    no_number = sorted((p["ref"] for p in listed if not p["mpn"].strip() and all(placeholder_any(p[d]) for d in DISTRIBUTORS)), key=_refkey)
    sides = {p["side"] for p in parts if p["bom"] and p["pos"]}
    reply = {"ok": True, "folder": str(out), "gerbers_zip": str(zpath), "files": sorted(f.name for f in gerb.iterdir()),
             "copper_layers": copper_layers(text), "bom": str(bom), "pos": str(pos), "assembled_sides": sorted(sides),
             "no_part_number": no_number, "fab": chosen, "zones": "refilled on the exported copy" if refill else "as saved"}
    if sides:
        try:
            reply |= assembly_drawing(board, out, sorted(sides, reverse=True))
        except ExportError as e:  # the drawing is an extra: the Gerbers, BOM and placement are written and good
            reply["assembly_drawing_error"] = str(e)
        if reply.get("not_on_assembly_drawing"):
            reply["assembly_drawing_note"] = ("these parts draw nothing on their side's fabrication layer (F.Fab, or B.Fab "
                                              "underneath), so the assembly drawing shows no outline or reference for them: "
                                              "add one to the footprint")
    check = ("upload the zip and look at the board house's Gerber viewer; for assembly, check each part's rotation against "
             "its pin 1 in the assembler's preview, since library footprints and the assembler's parts don't always agree")
    if chosen["fab"] == "jlcpcb":
        jb, jc = out / f"{stem}-bom-jlc.csv", out / f"{stem}-cpl-jlc.csv"
        jb.write_text(jlc_bom(parts), encoding="utf-8")
        jc.write_text(jlc_cpl(kicad_pos, parts), encoding="utf-8")
        reply |= {"jlc_bom": str(jb), "jlc_cpl": str(jc),
                  "no_lcsc_number": sorted((p["ref"] for p in listed if placeholder(p["lcsc"])), key=_refkey)}
    elif chosen["fab"] in fabmod.UPLOAD:
        reply["note"] = fabmod.UPLOAD[chosen["fab"]]
    elif chosen["house"]:
        reply["note"] = (f"cadmate doesn't know {chosen['house']}'s own BOM columns: upload the generic BOM and placement "
                         "files, and match the column names to their template if they ask for one")
    elif chosen["from"] == "not asked":
        reply["note"] = "no board house chosen yet: the generic files suit any; ask the user (ask_user) to get its own format too"
    reply["check"] = check + ("; parts on both sides means double-sided assembly, priced higher" if len(sides) > 1 else "")
    (out / "export.json").write_text(json.dumps({"board": stem, "board_sha256": source_sha}, indent=2) + "\n",
                                     encoding="utf-8")
    return reply


def placeholder_any(number: str) -> bool:
    """An empty or unfinished distributor number (TBD, with a question mark)."""
    n = number.strip()
    return not n or "?" in n or n.lower() in ("tbd", "todo", "n/a", "na", "-")


def step_target(board: Path, part: str | None = None) -> Path:
    """Where a board's STEP goes. In a cadmate project with FreeCAD: <part>/pcb-models/<board>.step, for the part
    named, or the only one there is; with KiCad only, or outside a project: beside the board."""
    from . import project

    root = project.find_root(board)
    sh = project.shape(root) if root else None
    if not root or "freecad" not in sh.tools:
        if part:
            raise ExportError(f"part {part!r} only applies in a cadmate project with FreeCAD; leave it out, or pass out")
        return board.with_suffix(".step")
    area = root / sh.dir("freecad")
    here, folders = project.units(root, sh, "freecad")
    if part and folders:
        if not (area / part).is_dir():
            raise ExportError(f"no FreeCAD part {part!r}; parts: {', '.join(f.name for f in folders)}")
        dest = area / part
    elif part:  # one part sits directly in the FreeCAD area: the name must be one of its files
        names = sorted({f.stem for f in here})
        if part not in names:
            raise ExportError(f"no FreeCAD part {part!r}" + (f"; the part here is {', '.join(names)}" if names else
                                                             "; there's no part file yet") + ". Leave part out: there's only one")
        dest = area
    elif len(folders) > 1:
        raise ExportError(f"which FreeCAD part is the board for? pass part: one of {', '.join(f.name for f in folders)}")
    else:
        dest = folders[0] if folders else area
    return dest / "pcb-models" / f"{board.stem}.step"


STEP_EXTS = (".step", ".stp", ".STEP", ".STP")
FAILED = re.compile(r"not found|cannot|can't|could not|couldn't|fail|unsupported|error|warning|missing", re.I)


def _model_path(raw: str, board: Path) -> Path | None:
    """A model link as a file, with ${KIPRJMOD} and set environment variables expanded; None while a variable is
    unknown here (kicad-cli may still know it, from KiCad's own settings)."""
    text = re.sub(r"\$\{(\w+)\}", lambda m: str(board.parent) if m[1] == "KIPRJMOD" else os.environ.get(m[1], m[0]), raw)
    if "${" in text:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else board.parent / path


def _model_problem(raw: str, board: Path) -> str | None:
    """Why a model link won't be in the STEP, as far as can be told without KiCad: missing, or VRML with no STEP."""
    path = _model_path(raw, board)
    if path is None:
        return None
    if path.suffix.lower() == ".wrl":  # --subst-models swaps in a STEP of the same name, when there is one
        if any(path.with_suffix(e).is_file() for e in STEP_EXTS):
            return None
        return "VRML only, no STEP beside it" if path.is_file() else "file not found"
    return None if path.is_file() else "file not found"


def _hidden(model: sexpr.Node) -> bool:
    h = model.child("hide")
    return "hide" in model.atoms() or (h is not None and "no" not in h.atoms())


def export_step(board: Path, out: Path | None = None, part: str | None = None) -> dict:
    """The board as a STEP with its parts' 3D models, for fitting the enclosure in FreeCAD. Written to out, else
    step_target's place. Parts without a 3D model are listed: they'd be missing from any collision check."""
    board = board.expanduser()
    if not board.exists():
        raise ExportError(f"no board at {board}")
    dest = out.expanduser() if out else step_target(board, part)
    dest.parent.mkdir(parents=True, exist_ok=True)
    printed = _cli(*versions.kicad_cli_args("pcb_step"), "--output", str(dest), str(board), timeout=600)
    if not dest.exists():
        raise ExportError(f"kicad-cli wrote no STEP at {dest}")
    lines = [ln.strip() for ln in printed.splitlines() if ln.strip()]
    bad = [ln for ln in lines if FAILED.search(ln)]  # kicad-cli's complaints, e.g. a model file it couldn't find
    root = sexpr.parse(board.read_text(encoding="utf-8", errors="replace"))
    no_model, not_loaded = [], {}
    for fp in root.children("footprint"):
        ref = _prop(fp, "Reference")
        attr = fp.child("attr")
        flags = set(attr.atoms()) if attr is not None else set()
        dnp = "dnp" in flags or (fp.child("dnp") is not None and "yes" in fp.child("dnp").atoms())
        if not ref or ref.startswith("#") or "board_only" in flags or dnp:  # --no-dnp leaves DNP parts out on purpose
            continue
        shown = [m for m in fp.children("model") if m.atoms() and not _hidden(m)]
        if not shown:
            no_model.append(ref)
            continue
        why = []
        for m in shown:
            raw = m.atoms()[0]
            name = Path(raw.replace("\\", "/")).name
            said = [ln for ln in bad if (name and name in ln) or (re.search(rf"\b{re.escape(ref)}\b", ln) and "model" in ln.lower())]
            problem = _model_problem(raw, board)
            if problem or said:
                why.append(f"{raw}: {problem or said[0]}")
        if why:  # a model that didn't make it in: that body is missing from the collision check
            not_loaded[ref] = "; ".join(why)
    reply = {"ok": True, "step": str(dest), "size_kb": round(dest.stat().st_size / 1024),
             "next": "bring it into the assembly beside the case parts, then check_enclosure for collisions and fit"}
    if no_model:
        reply["no_3d_model"] = sorted(no_model, key=_refkey)
    if not_loaded:
        reply["model_not_loaded"] = dict(sorted(not_loaded.items(), key=lambda kv: _refkey(kv[0])))
    if no_model or not_loaded:
        reply["note"] = ("these parts are missing from the STEP, so they're missing from any collision check too: "
                         "no_3d_model have no model (or only hidden ones), model_not_loaded link one that couldn't be "
                         "read. Fix the link, or make a model with the kicad-3d-models flow if the part is tall or near "
                         "the case")
    if bad:
        reply["kicad_cli_said"] = bad[:20]
    return reply
