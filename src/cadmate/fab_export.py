"""Manufacturing files for a finished board: Gerbers and drill files zipped for upload, a BOM and a placement file.

Gerbers, drill and positions come from kicad-cli, so they are exactly what KiCad's plot dialog would give. The BOM is
read from the board's footprints (value, footprint, and the part-number fields the schematic gave them); parts marked
DNP or excluded from the BOM are left out, and parts with no part number are listed so the order isn't short.

No board house is assumed. The generic BOM (manufacturer and part number first, any distributor numbers beside them)
and placement file go to any assembler. When the board house is one cadmate knows the columns for (JLCPCB), its own BOM
and CPL are written as well.
"""

from __future__ import annotations

import csv
import io
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
    r = subprocess.run([cli, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise ExportError(f"kicad-cli {args[0]} {args[1]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


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
    """Each footprint's reference, value, footprint name, part numbers (manufacturer, MPN, LCSC, DigiKey, Mouser), side,
    and whether it goes in the BOM / CPL."""
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
    for old in (f"{stem}-pos-kicad.csv", f"{stem}-bom-jlc.csv", f"{stem}-cpl-jlc.csv"):  # a previous export's, maybe for another house
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
    check = ("upload the zip and look at the board house's Gerber viewer; for assembly, check each part's rotation against "
             "its pin 1 in the assembler's preview, since library footprints and the assembler's parts don't always agree")
    if chosen["fab"] == "jlcpcb":
        jb, jc = out / f"{stem}-bom-jlc.csv", out / f"{stem}-cpl-jlc.csv"
        jb.write_text(jlc_bom(parts), encoding="utf-8")
        jc.write_text(jlc_cpl(kicad_pos, parts), encoding="utf-8")
        reply |= {"jlc_bom": str(jb), "jlc_cpl": str(jc),
                  "no_lcsc_number": sorted((p["ref"] for p in listed if placeholder(p["lcsc"])), key=_refkey)}
    elif chosen["house"]:
        reply["note"] = (f"cadmate doesn't know {chosen['house']}'s own BOM columns: upload the generic BOM and placement "
                         "files, and match the column names to their template if they ask for one")
    elif chosen["from"] == "not asked":
        reply["note"] = "no board house chosen yet: the generic files suit any; ask the user (ask_user) to get its own format too"
    reply["check"] = check + ("; parts on both sides means double-sided assembly, priced higher" if len(sides) > 1 else "")
    return reply


def placeholder_any(number: str) -> bool:
    """An empty or unfinished distributor number (TBD, with a question mark)."""
    n = number.strip()
    return not n or "?" in n or n.lower() in ("tbd", "todo", "n/a", "na", "-")
