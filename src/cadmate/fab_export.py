"""Manufacturing files for a finished board: Gerbers and drill files zipped for upload, and the BOM and placement (CPL)
files in the fab's assembly format.

Gerbers, drill and positions come from kicad-cli, so they are exactly what KiCad's plot dialog would give. The BOM is
read from the board's footprints (value, footprint, and the LCSC field the schematic gave them); parts marked DNP or
excluded from the BOM are left out, and parts with no LCSC number are listed so the order isn't short.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import sexpr
from .apps import KICAD_CLI_ENV, find_kicad_cli

GERBER_LAYERS = ["F.Cu", "B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "Edge.Cuts"]
INNER = ["In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu"]


class ExportError(RuntimeError):
    pass


def _cli(*args: str, timeout: int = 300) -> str:
    cli = find_kicad_cli()
    if not cli:
        raise ExportError(f"kicad-cli not found; set {KICAD_CLI_ENV}")
    r = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ExportError(f"kicad-cli {args[0]} {args[1]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def _prop(fp: sexpr.Node, name: str) -> str:
    for p in fp.children("property"):
        a = p.atoms()
        if a and a[0] == name:
            return a[1] if len(a) > 1 else ""
    return ""


def footprints(text: str) -> list[dict]:
    """Each footprint's reference, value, footprint name, LCSC number, side, and whether it goes in the BOM / CPL."""
    root = sexpr.parse(text)
    out = []
    for fp in root.children("footprint"):
        lib = (fp.atoms() or [""])[0]
        attr = fp.child("attr")
        flags = set(attr.atoms()) if attr is not None else set()
        dnp = "dnp" in flags or (fp.child("dnp") is not None and "yes" in fp.child("dnp").atoms())
        layer = (fp.child("layer").atoms() or ["F.Cu"])[0] if fp.child("layer") is not None else "F.Cu"
        out.append({"ref": _prop(fp, "Reference"), "value": _prop(fp, "Value"), "footprint": lib.split(":")[-1], "lcsc": _prop(fp, "LCSC"),
                    "side": "bottom" if layer == "B.Cu" else "top", "smd": "smd" in flags,
                    "bom": not dnp and "exclude_from_bom" not in flags and "board_only" not in flags,
                    "pos": not dnp and "exclude_from_pos_files" not in flags and "board_only" not in flags})
    return out


def _refkey(r: str):
    head = r.rstrip("0123456789")
    tail = r[len(head):]
    return (head, int(tail) if tail else 0)


def jlc_bom(parts: list[dict]) -> str:
    """JLCPCB's BOM: Comment, Designator, Footprint, LCSC Part #, one line per value + footprint + part number."""
    groups: dict[tuple, list[str]] = {}
    for p in parts:
        if p["bom"] and p["ref"] and not p["ref"].startswith("#"):
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


def export(board: Path, out_dir: Path | None = None, refill: bool = True) -> dict:
    """Gerbers + drill (zipped), BOM and CPL for JLCPCB, into out_dir (default: <project>/fab/<board name> inside a cadmate
    project, else <board folder>/fab/<board name>). The folder is regenerated: earlier Gerbers in it are removed first, so
    a layer the board no longer has can't ride along in the zip. With refill, the zones are refilled on a copy first:
    saved fills go stale after any edit, and the Gerbers are the fills."""
    from . import project

    board = board.expanduser()
    if not board.exists():
        raise ExportError(f"no board at {board}")
    root = project.find_root(board)
    out = out_dir.expanduser() if out_dir else (root / "fab" / board.stem if root else board.parent / "fab" / board.stem)
    gerb = out / "gerbers"
    if gerb.exists():
        shutil.rmtree(gerb)
    gerb.mkdir(parents=True, exist_ok=True)
    if refill:
        from .freerouting import refill as _refill

        copy = Path(tempfile.mkdtemp(prefix="cadmate-fab-")) / board.name  # not in the fab folder: it isn't for the fab
        shutil.copyfile(board, copy)
        for ext in (".kicad_pro", ".kicad_dru"):
            if board.with_suffix(ext).exists():
                shutil.copyfile(board.with_suffix(ext), copy.with_suffix(ext))
        try:
            _refill(copy)
        except Exception as e:  # noqa: BLE001
            raise ExportError(f"couldn't refill the zones ({e}); refill in KiCad (B), save, and export with refill=False") from None
        board = copy
    text = board.read_text(errors="replace")
    layers = [l for l in GERBER_LAYERS if l != "F.Cu" and l != "B.Cu"] + copper_layers(text)
    _cli("pcb", "export", "gerbers", "--output", str(gerb) + "/", "--layers", ",".join(layers), "--subtract-soldermask", str(board))
    _cli("pcb", "export", "drill", "--output", str(gerb) + "/", "--format", "excellon", "--excellon-separate-th",
         "--generate-map", "--map-format", "gerberx2", str(board))
    zpath = out / f"{board.stem}-gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(gerb.iterdir()):
            z.write(f, f.name)
    pos = out / f"{board.stem}-pos-kicad.csv"
    _cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--output", str(pos), str(board))
    parts = footprints(text)
    (out / f"{board.stem}-bom-jlc.csv").write_text(jlc_bom(parts))
    (out / f"{board.stem}-cpl-jlc.csv").write_text(jlc_cpl(pos.read_text(), parts))
    no_lcsc = sorted((p["ref"] for p in parts if p["bom"] and placeholder(p["lcsc"]) and p["ref"] and not p["ref"].startswith("#")), key=_refkey)
    sides = {p["side"] for p in parts if p["bom"] and p["pos"]}
    return {"ok": True, "folder": str(out), "gerbers_zip": str(zpath), "files": sorted(f.name for f in gerb.iterdir()),
            "copper_layers": copper_layers(text), "bom": str(out / f"{board.stem}-bom-jlc.csv"),
            "cpl": str(out / f"{board.stem}-cpl-jlc.csv"), "assembled_sides": sorted(sides), "no_lcsc_number": no_lcsc,
            "zones": "refilled on the exported copy" if refill else "as saved",
            "check": "upload the zip and look at JLC's Gerber viewer; in the assembly step check each part's rotation "
                     "against its pin 1 in JLC's preview, since library footprints and JLC's parts don't always agree"
                     + ("; parts on both sides means double-sided assembly, priced higher" if len(sides) > 1 else "")}
