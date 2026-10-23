"""Package families and the datasheet dimensions each one needs.

`check_params` turns the numbers an agent read off a datasheet into a checked
parameter set: missing values, impossible combinations, and mismatches with
the footprint are reported before any 3D geometry is built.
All dimensions are in mm.
"""

from __future__ import annotations

from .footprint import Footprint

FAMILIES: dict[str, dict] = {
    "soic": {
        "description": "Gull-wing leads on two sides: SOIC, SOP, SSOP, TSSOP, MSOP",
        "required": ["pins", "pitch", "body_length", "body_width", "overall_width", "height", "standoff", "lead_width", "lead_length"],
        "optional": [],
    },
    "sot": {
        "description": "Small outline transistor style: SOT-23, SOT-223, SC-70",
        "required": ["pins", "pitch", "body_length", "body_width", "overall_width", "height", "standoff", "lead_width", "lead_length"],
        "optional": [],
    },
    "qfn": {
        "description": "No-lead packages with pads on four sides: QFN, DFN (set pins_x/pins_y for non-square)",
        "required": ["pins", "pitch", "body_length", "body_width", "height", "lead_width", "lead_length"],
        "optional": ["ep_length", "ep_width", "pins_x", "pins_y"],
    },
    "pin_header": {
        "description": "Straight pin headers and sockets",
        "required": ["pins_per_row", "rows", "pitch", "body_height", "pin_length_above", "pin_length_below"],
        "optional": ["pin_width"],
    },
    "radial_cap": {
        "description": "Radial through-hole electrolytic or film capacitor",
        "required": ["diameter", "height", "lead_spacing", "lead_diameter"],
        "optional": [],
    },
    "box": {
        "description": "Plain body for anything else; refine by hand in FreeCAD",
        "required": ["length", "width", "height"],
        "optional": ["standoff"],
    },
}

PITCH_TOLERANCE = 0.02


def check_params(family: str, params: dict, footprint: Footprint | None = None) -> dict:
    if family not in FAMILIES:
        return {"ok": False, "errors": [f"unknown family {family!r}; known: {sorted(FAMILIES)}"], "warnings": []}
    spec = FAMILIES[family]
    errors: list[str] = []
    warnings: list[str] = []
    p: dict[str, float] = {}

    for key in spec["required"] + spec["optional"]:
        if key not in params or params[key] is None:
            if key in spec["required"]:
                errors.append(f"missing {key}")
            continue
        try:
            p[key] = float(params[key])
        except (TypeError, ValueError):
            errors.append(f"{key} is not a number: {params[key]!r}")
            continue
        if p[key] <= 0:
            errors.append(f"{key} must be positive, got {p[key]}")
    unknown = sorted(set(params) - set(spec["required"]) - set(spec["optional"]))
    if unknown:
        warnings.append(f"ignored unknown parameters: {unknown}")
    if errors:
        return {"ok": False, "family": family, "errors": errors, "warnings": warnings}

    def rule(cond: bool, msg: str):
        if not cond:
            errors.append(msg)

    if family in ("soic", "sot"):
        rule(p["standoff"] < p["height"], "standoff must be below the total height")
        rule(p["overall_width"] > p["body_width"], "overall_width (tip to tip) must exceed body_width")
        rule(p["lead_width"] < p["pitch"], "lead_width must be smaller than pitch")
        if family == "soic":
            rule(int(p["pins"]) % 2 == 0, "soic needs an even pin count")
            rule((p["pins"] / 2 - 1) * p["pitch"] < p["body_length"], "leads on one side do not fit along body_length")
    if family == "qfn":
        rule(p["lead_width"] < p["pitch"], "lead_width must be smaller than pitch")
        if "ep_length" in p:
            rule(p["ep_length"] < p["body_length"], "exposed pad longer than the body")
        if "ep_width" in p:
            rule(p["ep_width"] < p["body_width"], "exposed pad wider than the body")
    if family == "radial_cap":
        rule(p["lead_spacing"] < p["diameter"], "lead_spacing should be inside the can diameter")

    expected_pins = None
    if family == "pin_header":
        expected_pins = int(p["pins_per_row"] * p["rows"])
    elif "pins" in p:
        expected_pins = int(p["pins"])
    elif family == "radial_cap":
        expected_pins = 2

    if footprint is not None:
        count = len(footprint.signal_pads())
        if expected_pins is not None and count not in (expected_pins, expected_pins + 1):
            errors.append(f"footprint has {count} numbered pads, datasheet says {expected_pins} pins")
        elif expected_pins is not None and count == expected_pins + 1:
            warnings.append("footprint has one extra pad, assumed to be an exposed/thermal pad")
        fp_pitch = footprint.pitch()
        if "pitch" in p and fp_pitch is not None and abs(fp_pitch - p["pitch"]) > PITCH_TOLERANCE:
            # Two-row parts with a short body can report the row spacing instead; only warn then.
            (warnings if family in ("sot", "pin_header") else errors).append(
                f"footprint pitch {fp_pitch} mm differs from datasheet pitch {p['pitch']} mm"
            )
        if family == "radial_cap" and fp_pitch is not None and abs(fp_pitch - p["lead_spacing"]) > PITCH_TOLERANCE:
            errors.append(f"footprint pad spacing {fp_pitch} mm differs from lead_spacing {p['lead_spacing']} mm")

    return {"ok": not errors, "family": family, "params": p, "errors": errors, "warnings": warnings}
