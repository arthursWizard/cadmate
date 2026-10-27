"""IPC-7351B land pattern maths.

Pads come from the package's toleranced dimensions:

    Zmax = Lmin + 2*Jt + sqrt(CL^2 + F^2 + P^2)   outer pad edge to outer pad edge
    Gmin = Smax - 2*Jh - sqrt(CS^2 + F^2 + P^2)   inner pad edge to inner pad edge
    Xmax = Wmin + 2*Js + sqrt(CW^2 + F^2 + P^2)   pad width

L is the lead span (tip to tip), S the span between the heels, W the lead
width, C* their tolerances, F the board fabrication tolerance and P the
placement tolerance. Jt, Jh, Js are the toe, heel and side solder fillets for
the lead style and density level. Datasheets rarely print S, so it is derived
from L and the lead length T; its tolerance is the RMS of the parts
(sqrt(CL^2 + 2*CT^2)) rather than the worst case sum, as IPC-7351B allows.

Results are rounded to 0.05 mm, the same convention the KiCad library uses,
so JEDEC packages come out with the same pads as KiCad's own footprints.
All values are in mm.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

FAB_TOLERANCE = 0.1  # F
PLACEMENT_TOLERANCE = 0.05  # P
ROUND_BASE = 0.05
DENSITIES = ("least", "nominal", "most")  # IPC levels C, B, A

# (toe, heel, side) fillets per density, from IPC-7351B tables 3-2, 3-3 and 3-15.
FILLETS = {
    "gullwing": {"least": (0.15, 0.25, 0.01), "nominal": (0.35, 0.35, 0.03), "most": (0.55, 0.45, 0.05)},
    "gullwing_fine": {"least": (0.15, 0.25, -0.04), "nominal": (0.35, 0.35, -0.02), "most": (0.55, 0.45, 0.01)},
    "no_lead": {"least": (0.2, 0.0, -0.04), "nominal": (0.3, 0.0, -0.04), "most": (0.4, 0.0, -0.04)},
}
FINE_PITCH = 0.625  # gull-wing pitch at or below this uses the fine-pitch table
COURTYARD = {"least": 0.1, "nominal": 0.25, "most": 0.5}


@dataclass(frozen=True)
class Dim:
    """A toleranced dimension. rms is the statistical tolerance; it equals max - min for a measured value."""

    min: float
    nom: float
    max: float
    rms: float | None = None

    @property
    def tol(self) -> float:
        return self.max - self.min

    @property
    def tol_rms(self) -> float:
        return self.tol if self.rms is None else self.rms

    @property
    def max_rms(self) -> float:
        return self.max - (self.tol - self.tol_rms) / 2

    @property
    def min_rms(self) -> float:
        return self.min + (self.tol - self.tol_rms) / 2

    def minus_twice(self, other: Dim) -> Dim:
        """self - 2 * other, e.g. heel span S = lead span L - 2 * lead length T."""
        rms = math.sqrt(self.tol_rms**2 + 2 * other.tol_rms**2)
        lo, hi = self.min - 2 * other.max, self.max - 2 * other.min
        return Dim(lo, self.nom - 2 * other.nom, hi, min(rms, hi - lo))


_NUM = r"[-+]?\d*\.?\d+"


def dim(value) -> Dim:
    """Read a dimension as a datasheet gives it.

    Accepted: 1.27 (exact), [min, max], [min, nom, max], {"min", "nom", "max"}
    or {"nom", "tol"}, and the strings "0.4..1.27", "0.31..0.41..0.51",
    "6.0+/-0.2" and "6.0±0.2". With only min and max, nominal is the middle.
    """
    if isinstance(value, Dim):
        return value
    if isinstance(value, bool):
        raise ValueError(f"not a dimension: {value!r}")
    if isinstance(value, (int, float)):
        return _checked(float(value), float(value), float(value))
    if isinstance(value, (list, tuple)):
        v = [float(x) for x in value]
        if len(v) == 1:
            return _checked(v[0], v[0], v[0])
        if len(v) == 2:
            return _checked(v[0], (v[0] + v[1]) / 2, v[1])
        if len(v) == 3:
            return _checked(v[0], v[1], v[2])
    if isinstance(value, dict):
        lo, nom, hi, tol = (value.get(k) for k in ("min", "nom", "max", "tol"))
        if nom is not None and tol is not None and lo is None and hi is None:
            return _checked(float(nom) - float(tol), float(nom), float(nom) + float(tol))
        if lo is not None and hi is not None:
            return _checked(float(lo), float(nom) if nom is not None else (float(lo) + float(hi)) / 2, float(hi))
        if nom is not None and lo is None and hi is None:
            return _checked(float(nom), float(nom), float(nom))
    if isinstance(value, str):
        s = value.replace(" ", "").replace("±", "+/-")
        if re.fullmatch(_NUM, s):
            return dim(float(s))
        m = re.fullmatch(rf"({_NUM})\+/-({_NUM})", s)
        if m:
            return dim({"nom": float(m[1]), "tol": float(m[2])})
        parts = s.split("..")
        if 2 <= len(parts) <= 3 and all(re.fullmatch(_NUM, p) for p in parts):
            return dim([float(p) for p in parts])
    raise ValueError(f"not a dimension: {value!r}")


def _checked(lo: float, nom: float, hi: float) -> Dim:
    if not lo <= nom <= hi:
        raise ValueError(f"min {lo} <= nominal {nom} <= max {hi} does not hold")
    return Dim(lo, nom, hi)


def round_to(value: float, base: float = ROUND_BASE) -> float:
    return round(math.floor(value / base + 0.5) * base, 6)


def ceil_to(value: float, base: float) -> float:
    return round(math.ceil(value / base - 1e-9) * base, 6)


def _rss(c: float) -> float:
    return math.sqrt(c**2 + FAB_TOLERANCE**2 + PLACEMENT_TOLERANCE**2)


@dataclass
class Land:
    """Pad row geometry across two opposite rows: outer span Z, inner span G, pad width X."""

    z: float
    g: float
    x: float

    @property
    def pad_length(self) -> float:
        return round((self.z - self.g) / 2, 6)

    @property
    def center(self) -> float:
        """Distance from the package centre to each pad centre."""
        return round((self.z + self.g) / 4, 6)


def land(lead_span: Dim, lead_length: Dim, lead_width: Dim, fillets: tuple[float, float, float], heel_reduction: float = 0.0) -> Land:
    """IPC-7351B pads for one pair of opposite rows. heel_reduction pulls the inner edge out (mm per side)."""
    toe, heel, side = fillets
    s = lead_span.minus_twice(lead_length)
    z = lead_span.min_rms + 2 * toe + _rss(lead_span.tol_rms)
    g = s.max_rms - 2 * heel + 2 * heel_reduction - _rss(s.tol_rms)
    x = lead_width.min_rms + 2 * side + _rss(lead_width.tol_rms)
    return Land(round_to(z), round_to(g), round_to(x))


# Through-hole: IPC-7251 / IPC-2222 hole allowance over the maximum lead size, and the pad
# addition over the hole (2 x 0.05 mm minimum annular ring plus the IPC-2221 fabrication allowance).
HOLE_ALLOWANCE = {"least": 0.15, "nominal": 0.2, "most": 0.25}
PAD_ADDITION = {"least": 0.5, "nominal": 0.6, "most": 0.7}
DRILL_STEP = 0.05  # holes and pads rounded up to this


@dataclass
class Hole:
    """A plated hole and its pad. drill_y is set for a slot (KiCad oval drill), long axis along X unless rotated."""

    drill: float
    pad_w: float
    pad_h: float
    drill_y: float | None = None


def tht_hole(density: str, lead_diameter: Dim | None = None, lead_width: Dim | None = None,
             lead_thickness: Dim | None = None, slot: bool = False) -> Hole:
    """Hole and pad for a through-hole lead.

    A round lead gets a round hole. A rectangular lead (width x thickness) gets a
    round hole that clears its diagonal, or with slot=True a slot that clears
    each side, which suits flat tabs much better.
    """
    allow, add = HOLE_ALLOWANCE[density], PAD_ADDITION[density]
    if lead_diameter is not None:
        if slot:
            raise ValueError("a slot needs a rectangular lead: give lead_width and lead_thickness")
        d = ceil_to(lead_diameter.max + allow, DRILL_STEP)
        p = ceil_to(d + add, DRILL_STEP)
        return Hole(d, p, p)
    if lead_width is None or lead_thickness is None:
        raise ValueError("give lead_diameter, or lead_width and lead_thickness")
    if slot:
        dx = ceil_to(lead_width.max + allow, DRILL_STEP)
        dy = ceil_to(lead_thickness.max + allow, DRILL_STEP)
        dx, dy = max(dx, dy), min(dx, dy)
        # The pad grows by the same ring all round the slot.
        return Hole(dx, ceil_to(dx + add, DRILL_STEP), ceil_to(dy + add, DRILL_STEP), dy if dy < dx else None)
    d = ceil_to(math.hypot(lead_width.max, lead_thickness.max) + allow, DRILL_STEP)
    p = ceil_to(d + add, DRILL_STEP)
    return Hole(d, p, p)
