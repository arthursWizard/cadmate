"""Build a 3D model (STEP) for a footprint from the same datasheet values, and check linked models.

The geometry is worked out here, in Python, from the generated footprint (its
pads and body box) and the package heights; FreeCAD only turns the resulting
list of boxes, cylinders and lead profiles into solids and writes the STEP
(freecad_scripts/build_model.py). So the model sits on the footprint by
construction: same origin, same pin 1, a lead on every pad.

Frame: mm, Z up, Z = 0 is the top of the board. KiCad's footprint Y points
down and the 3D Y points up, so a pad at footprint (x, y) is at 3D (x, -y).
The models follow KiCad's library conventions: body and leads are separate
solids (body dark, leads metal), the body has a pin 1 dimple, SMD parts sit
on the board, through-hole leads go below it.

Families:
- soic:   body from the standoff to the height, gull-wing leads from the lead tip (overall_width) to the body
- qfn:    body on the board, terminals and exposed pad flush with its underside
- custom: a body box over the footprint's body outline, with a lead in every through-hole pad (round,
          or square/flat with lead_width and lead_thickness; leads beside the body, as on a DIP, get a
          shoulder into its side; lead_above makes header posts) and a terminal under every SMD pad. A placeholder good enough for clearance and enclosure work;
          model a module or connector properly in FreeCAD when its looks matter.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from . import apps, check, ipc
from .footprint import BBox, Footprint, Model, Pad

BODY = (0.15, 0.15, 0.15)
METAL = (0.82, 0.82, 0.84)
LEAD_THICKNESS = 0.2  # JEDEC c, when the datasheet gives none
TERMINAL_THICKNESS = 0.2  # QFN A3
LEAD_BELOW = 3.0  # through-hole leads below the board, KiCad's usual
DIMPLE_DEPTH = 0.1

# Which datasheet values each family's model needs, on top of the footprint's.
FAMILIES: dict[str, dict] = {
    "soic": {"required": ["height", "standoff"], "optional": ["lead_thickness"]},
    "qfn": {"required": ["height"], "optional": ["terminal_thickness"]},
    "custom": {"required": ["height"], "optional": ["standoff", "lead_below", "lead_above", "lead_diameter", "lead_width", "lead_thickness"]},
}

_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _nom(params: dict, key: str, default: float | None = None) -> float | None:
    v = params.get(key)
    return default if v is None else ipc.dim(v).nom


def _fy(y: float) -> float:
    """Footprint Y (down) to 3D Y (up)."""
    return -y


def _box(name: str, x0: float, y0: float, z0: float, dx: float, dy: float, dz: float, colour=METAL, cut=None) -> dict:
    s = {"name": name, "colour": colour, "box": [round(v, 6) for v in (x0, y0, z0, dx, dy, dz)]}
    if cut:
        s["cut"] = cut
    return s


def _dimple(body: BBox, pin1: Pad | None, top: float) -> dict:
    """A shallow cylinder cut into the top of the body at the pin 1 corner (3D frame)."""
    w, h = body.width, body.height
    r = round(max(0.15, min(0.5, min(w, h) / 10)), 3)
    cx, cy = body.center
    px, py = (pin1.x, _fy(pin1.y)) if pin1 is not None else (body.xmin, body.ymax)
    sx = -1 if px <= cx else 1
    sy = -1 if py <= cy else 1
    inset = r + min(0.3, min(w, h) / 10)
    x = cx + sx * (w / 2 - inset)
    y = cy + sy * (h / 2 - inset)
    return {"cyl": [round(x, 6), round(y, 6), round(top - DIMPLE_DEPTH, 6), r, DIMPLE_DEPTH + 0.01]}


def _body3d(fp_body: BBox) -> BBox:
    return BBox(fp_body.xmin, _fy(fp_body.ymax), fp_body.xmax, _fy(fp_body.ymin))


def plan(family: str, params: dict, fp: Footprint, body: BBox) -> dict:
    """The solids of the model, in the 3D frame. body is the footprint's body box (footprint frame).

    Returns {"solids": [...], "bbox": {...}, "expected": {length, width, height}}; raises ValueError for
    missing or impossible values.
    """
    spec = FAMILIES.get(family)
    if spec is None:
        raise ValueError(f"no model builder for family {family!r}; known: {sorted(FAMILIES)}")
    missing = [k for k in spec["required"] if params.get(k) is None]
    if missing:
        raise ValueError(f"the 3D model needs {', '.join(missing)} from the datasheet")
    b3 = _body3d(body)
    pads = [p for p in fp.pads if p.number and p.kind != "np_thru_hole" and any("Cu" in layer for layer in p.layers)]
    if family == "soic":
        solids, expected = _soic(params, b3, pads, fp)
    elif family == "qfn":
        solids, expected = _qfn(params, b3, pads, fp)
    else:
        solids, expected = _custom(params, b3, pads, fp)
    return {"solids": solids, "bbox": bbox(solids), "expected": expected}


def _soic(params: dict, b: BBox, pads: list[Pad], fp: Footprint):
    height, standoff = _nom(params, "height"), _nom(params, "standoff")
    tip = _nom(params, "overall_width") / 2
    foot = _nom(params, "lead_length")
    lead_w = _nom(params, "lead_width")
    c = _nom(params, "lead_thickness", LEAD_THICKNESS)
    edge = b.width / 2
    if not 0 <= standoff < height:
        raise ValueError("standoff must be at least 0 and below the height")
    if tip - foot <= edge:
        raise ValueError("the feet reach under the body: check overall_width, lead_length and body_width")
    exit_z = standoff + (height - standoff) / 2  # leads leave the body at mid height
    if exit_z - c / 2 <= c:
        raise ValueError("body too thin for leads of this thickness; give lead_thickness")
    top = height
    solids = [_box("Body", b.xmin, b.ymin, standoff, b.width, b.height, height - standoff, BODY, [_dimple(b, fp.pin1, top)])]
    for p in pads:
        s = -1 if p.x < 0 else 1
        t, f, e = s * tip, s * (tip - foot), s * edge
        m = (f + e) / 2
        # Profile in the XZ plane: flat foot, bend up, shoulder into the body; extruded along Y.
        pts = [(t, 0), (f, 0), (m, exit_z - c / 2), (e, exit_z - c / 2), (e, exit_z + c / 2), (m, exit_z + c / 2), (f, c), (t, c)]
        y = _fy(p.y)
        solids.append({"name": f"Lead_{p.number}", "colour": METAL, "prism": {"pts": [[round(x, 6), round(z, 6)] for x, z in pts],
                                                                            "y0": round(y - lead_w / 2, 6), "dy": round(lead_w, 6)}})
    return solids, {"length": round(2 * tip, 4), "width": round(b.height, 4), "height": height}


def _qfn(params: dict, b: BBox, pads: list[Pad], fp: Footprint):
    height = _nom(params, "height")
    t = _nom(params, "terminal_thickness", TERMINAL_THICKNESS)
    length = _nom(params, "lead_length")
    lead_w = _nom(params, "lead_width")
    if t >= height:
        raise ValueError("terminal_thickness must be below the height")
    terminals = []
    for p in pads:
        y = _fy(p.y)
        if abs(p.x) < b.width / 2 - length and abs(y) < b.height / 2 - length:  # the exposed pad
            terminals.append(_box(f"EP_{p.number}", p.x - p.width / 2, y - p.height / 2, 0, p.width, p.height, t))
        elif abs(p.x) / max(b.width, 1e-9) >= abs(y) / max(b.height, 1e-9):  # left and right sides
            x0 = b.xmin if p.x < 0 else b.xmax - length
            terminals.append(_box(f"Lead_{p.number}", x0, y - lead_w / 2, 0, length, lead_w, t))
        else:  # top and bottom
            y0 = b.ymin if y < 0 else b.ymax - length
            terminals.append(_box(f"Lead_{p.number}", p.x - lead_w / 2, y0, 0, lead_w, length, t))
    # The terminals are cut out of the body, so the solids touch without overlapping.
    cut = [{"box": s["box"]} for s in terminals] + [_dimple(b, fp.pin1, height)]
    body = _box("Body", b.xmin, b.ymin, 0, b.width, b.height, height, BODY, cut)
    return [body] + terminals, {"length": round(b.width, 4), "width": round(b.height, 4), "height": height}


def _custom(params: dict, b: BBox, pads: list[Pad], fp: Footprint):
    height = _nom(params, "height")
    standoff = _nom(params, "standoff", 0.0)
    below = _nom(params, "lead_below", LEAD_BELOW)
    above = _nom(params, "lead_above", 0.0)
    lead_d = _nom(params, "lead_diameter")
    lead_w, lead_t = _nom(params, "lead_width"), _nom(params, "lead_thickness")
    if not 0 <= standoff < height:
        raise ValueError("standoff must be at least 0 and below the height")
    leads, cut = [], [_dimple(b, fp.pin1, height)]
    shoulder_z = standoff + min(0.5, (height - standoff) / 3)  # where leads outside the body (DIP) leave its side
    for p in pads:
        x, y = p.x, _fy(p.y)
        if p.kind == "thru_hole" and p.drill:
            if below <= 0:
                continue
            inside = b.xmin < x < b.xmax and b.ymin < y < b.ymax
            # A lead under the body reaches its underside, or through it for header posts; one beside the body
            # rises to a shoulder that runs into the body's side.
            top = (height + above) if inside and above > 0 else (standoff if inside else shoulder_z)
            if p.drill_y:  # slot: a flat lead
                w, h = max(0.2, p.drill - 0.2), max(0.2, p.drill_y - 0.2)
                if p.rotation % 180:
                    w, h = h, w
            elif lead_w is not None and lead_t is not None:  # square or flat leads
                w, h = lead_w, lead_t
            else:
                w = h = None
            if w is not None:
                leads.append(_box(f"Lead_{p.number}", x - w / 2, y - h / 2, -below, w, h, below + top))
            else:
                d = lead_d if lead_d is not None else max(0.3, p.drill - 0.2)  # IPC-7251 nominal hole is lead + 0.2
                w = h = d
                leads.append({"name": f"Lead_{p.number}", "colour": METAL, "cyl": [round(x, 6), round(y, 6), -below, round(d / 2, 6), round(below + top, 6)]})
            if not inside:
                t = min(w, h)
                if x <= b.xmin or x >= b.xmax:
                    x0, x1 = (x - w / 2, b.xmin) if x <= b.xmin else (b.xmax, x + w / 2)
                    leads.append(_box(f"Shoulder_{p.number}", x0, y - h / 2, top - t, x1 - x0, h, t))
                else:
                    y0, y1 = (y - h / 2, b.ymin) if y <= b.ymin else (b.ymax, y + h / 2)
                    leads.append(_box(f"Shoulder_{p.number}", x - w / 2, y0, top - t, w, y1 - y0, t))
        elif p.kind == "smd":
            hw, hh = p.half_size
            t = standoff if standoff > 0 else 0.1
            box = _box(f"Lead_{p.number}", x - hw, y - hh, 0, 2 * hw, 2 * hh, t)
            leads.append(box)
            if standoff == 0:
                cut.append({"box": box["box"]})
    body = _box("Body", b.xmin, b.ymin, standoff, b.width, b.height, height - standoff, BODY, cut)
    solids = [body] + leads
    # Leads beside the body make the part wider than the body; the expected size is what the leads span.
    span = bbox(solids)
    return solids, {"length": round(span["xmax"] - span["xmin"], 4), "width": round(span["ymax"] - span["ymin"], 4), "height": height + above}


def bbox(solids: list[dict]) -> dict:
    """Bounding box of the planned solids, as FreeCAD would report it for the STEP."""
    xs, ys, zs = [], [], []
    for s in solids:
        if "box" in s:
            x, y, z, dx, dy, dz = s["box"]
            xs += [x, x + dx]
            ys += [y, y + dy]
            zs += [z, z + dz]
        elif "cyl" in s:
            x, y, z, r, h = s["cyl"]
            xs += [x - r, x + r]
            ys += [y - r, y + r]
            zs += [z, z + h]
        elif "prism" in s:
            pr = s["prism"]
            xs += [p[0] for p in pr["pts"]]
            zs += [p[1] for p in pr["pts"]]
            ys += [pr["y0"], pr["y0"] + pr["dy"]]
    return {"xmin": min(xs), "xmax": max(xs), "ymin": min(ys), "ymax": max(ys), "zmin": min(zs), "zmax": max(zs)}


def build(solids: list[dict], out: Path, name: str) -> dict:
    """Write the planned solids as a STEP file through FreeCADCmd. Returns what FreeCAD measured."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "model.json"
        spec.write_text(json.dumps({"name": name, "solids": solids, "out": str(out)}))
        return apps.run_freecad_script(Path(__file__).parent / "freecad_scripts" / "build_model.py", env={"CADMATE_SPEC": str(spec)})


def resolve(path: str, footprint_dir: Path | None = None, project_dir: Path | None = None) -> Path:
    """Expand ${VARS} (KiCad style) from the environment.

    ${KIPRJMOD} falls back to the project folder, else the folder above the footprint's .pretty
    library (where KiCad projects keep their libraries), else the footprint's own folder.
    """

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in os.environ:
            return os.environ[name]
        if name == "KIPRJMOD":
            if project_dir is not None:
                return str(project_dir)
            if footprint_dir is not None:
                return str(footprint_dir.parent if footprint_dir.suffix == ".pretty" else footprint_dir)
        raise ValueError(f"${{{name}}} is not set; pass model_path explicitly or set it in the server's environment")

    return Path(_VAR.sub(sub, path)).expanduser()


def check_file(fp: Footprint, model_file: Path, placement: Model, expected: dict | None = None, cutout: bool = False) -> dict:
    """Check a STEP file placed on a footprint: read its box in FreeCAD, then check.check_model."""
    if model_file.suffix.lower() in (".wrl", ".vrml"):
        step = model_file.with_suffix(".step")
        if not step.exists():
            return {"ok": False, "errors": [f"{model_file.name} is VRML; checks need the STEP next to it ({step.name})"], "warnings": []}
        model_file = step
    if not model_file.exists():
        return {"ok": False, "errors": [f"model file not found: {model_file}"], "warnings": []}
    info = apps.run_freecad_script(Path(__file__).parent / "freecad_scripts" / "inspect_model.py", env={"CADMATE_MODEL": str(model_file)})
    result = check.check_model(fp, info["bbox"], placement, expected or None, cutout)
    if not info.get("valid", True):
        result["errors"].append("FreeCAD reports the shape as invalid")
    if info.get("solids", 1) == 0:
        result["errors"].append("model contains no solids")
    result["ok"] = not result["errors"]
    result["model_file"] = str(model_file)
    result["solids"] = info.get("solids")
    return result
