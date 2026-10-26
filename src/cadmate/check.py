"""Check a 3D model's placement against its footprint.

The model's bounding box (from FreeCAD, in the model's own frame) is moved by
the footprint's model offset and compared with the pads and courtyard. KiCad's
footprint Y axis points down while the 3D Y axis points up, so pad Y values
are flipped before comparing. Rotations other than steps of 90 degrees are not
handled here yet (they would need the rotated shape, not its box).
"""

from __future__ import annotations

from .footprint import Footprint, Model

CENTER_TOLERANCE = 0.25  # mm
Z_TOLERANCE = 0.05  # mm


def _rotate90(v: tuple[float, float, float], axis: int, quarter_turns: int) -> tuple[float, float, float]:
    """Rotate v by quarter_turns * 90 degrees counter-clockwise about axis (0=X, 1=Y, 2=Z)."""
    i, j = [(1, 2), (2, 0), (0, 1)][axis]
    v = list(v)
    for _ in range(quarter_turns % 4):
        v[i], v[j] = -v[j], v[i]
    return tuple(v)


def _apply_placement(bbox: dict, model: Model) -> dict:
    """Place the model's bounding box as KiCad does: scale, rotate X, then Y, then Z, then offset.

    KiCad stores the rotation angles negated (a file value of -90 about X turns
    a Y-up model so its +Y points to +Z), so each angle is applied as -angle.
    """
    sx, sy, sz = model.scale
    corners = [
        (x * sx, y * sy, z * sz)
        for x in (bbox["xmin"], bbox["xmax"])
        for y in (bbox["ymin"], bbox["ymax"])
        for z in (bbox["zmin"], bbox["zmax"])
    ]
    for axis in (0, 1, 2):
        turns = round(-model.rotate[axis] / 90)
        corners = [_rotate90(c, axis, turns) for c in corners]
    ox, oy, oz = model.offset
    return {
        "xmin": min(c[0] for c in corners) + ox, "xmax": max(c[0] for c in corners) + ox,
        "ymin": min(c[1] for c in corners) + oy, "ymax": max(c[1] for c in corners) + oy,
        "zmin": min(c[2] for c in corners) + oz, "zmax": max(c[2] for c in corners) + oz,
    }


def check_model(footprint: Footprint, bbox: dict, model: Model | None = None, expected: dict | None = None) -> dict:
    """Compare a model bounding box with the footprint.

    bbox: xmin..zmax of the model as read from its STEP file.
    model: the placement from the footprint (offset/scale/rotate); identity if None.
    expected: optional body size from the datasheet, keys length/width/height (mm).
    """
    model = model or Model(path="")
    if any(abs(r - 90 * round(r / 90)) > 1e-6 for r in model.rotate):
        return {"ok": False, "errors": ["only rotations in steps of 90 degrees are checked so far"], "warnings": [], "measured": {}}
    b = _apply_placement(bbox, model)
    errors: list[str] = []
    warnings: list[str] = []

    pads = footprint.pad_extents
    # Footprint frame -> 3D frame: y_3d = -y_fp
    pad_box = None if pads is None else {"xmin": pads.xmin, "xmax": pads.xmax, "ymin": -pads.ymax, "ymax": -pads.ymin}
    measured = {"model_bbox_placed": b, "pad_extents_3d": pad_box}

    if footprint.smd and pad_box:
        mcx, mcy = (b["xmin"] + b["xmax"]) / 2, (b["ymin"] + b["ymax"]) / 2
        pcx, pcy = (pad_box["xmin"] + pad_box["xmax"]) / 2, (pad_box["ymin"] + pad_box["ymax"]) / 2
        measured["center_offset"] = (round(mcx - pcx, 4), round(mcy - pcy, 4))
        if abs(mcx - pcx) > CENTER_TOLERANCE or abs(mcy - pcy) > CENTER_TOLERANCE:
            errors.append(f"model centre is ({mcx - pcx:+.2f}, {mcy - pcy:+.2f}) mm from the pad centre")
        if b["zmin"] < -Z_TOLERANCE:
            errors.append(f"SMD model goes {-b['zmin']:.2f} mm below the board surface")
        if b["zmin"] > Z_TOLERANCE:
            warnings.append(f"model floats {b['zmin']:.2f} mm above the board; fine only if the leads are separate solids that touch the pads")
    if not footprint.smd:
        pin1 = footprint.pin1
        if pin1 is not None and (abs(pin1.x) > 1e-3 or abs(pin1.y) > 1e-3):
            warnings.append("through-hole footprint whose pin 1 is not at the origin (KiCad library convention puts it there)")
        if b["zmin"] > Z_TOLERANCE:
            warnings.append("through-hole model has no part below the board; leads may be missing")

    court = footprint.courtyard
    if court is not None:
        c3 = {"xmin": court.xmin, "xmax": court.xmax, "ymin": -court.ymax, "ymax": -court.ymin}
        over = max(c3["xmin"] - b["xmin"], b["xmax"] - c3["xmax"], c3["ymin"] - b["ymin"], b["ymax"] - c3["ymax"])
        measured["courtyard_3d"] = c3
        if over > 0.01:
            warnings.append(f"model extends {over:.2f} mm outside the courtyard")

    if expected:
        size = {"length": b["xmax"] - b["xmin"], "width": b["ymax"] - b["ymin"], "height": b["zmax"] - max(b["zmin"], 0.0)}
        measured["size"] = {k: round(v, 4) for k, v in size.items()}
        for key in ("length", "width", "height"):
            if key in expected:
                # Overall model size includes leads, so it may only be larger than the body, by a bounded amount.
                diff = size[key] - float(expected[key])
                if diff < -0.05:
                    errors.append(f"model {key} {size[key]:.2f} mm is smaller than the datasheet {expected[key]} mm")
                elif key == "height" and diff > 0.1:
                    errors.append(f"model height {size[key]:.2f} mm exceeds the datasheet {expected[key]} mm")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "measured": measured}
