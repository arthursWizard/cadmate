"""Does the part being bought fit the footprint on the board?

A distributor lists a part's package as text ("0805", "SOT-23-5", "QFN-32-EP(5x5)", "SOP-8"); the board has a
footprint name ("R_0603_1608Metric", "SOT-23-6", "QFN-32-1EP_4x4mm_P0.4mm_EP2.7x2.7mm"). Both are read into the same
few facts (chip size, package family, pin count, body size) and compared. Only facts both sides state are compared, so
an answer of "match" means nothing that was stated disagrees, and text that can't be read gives "unknown", never a
guess.
"""

from __future__ import annotations

import re

# chip sizes: imperial code -> metric code
CHIP = {"01005": "0402", "0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216",
        "1210": "3225", "1812": "4532", "2010": "5025", "2512": "6332"}
METRIC = {m: i for i, m in CHIP.items()}

# names for the same package, to one family name (and a pin count where the name implies one)
ALIASES = {
    "SO": "SOIC", "SOP": "SOIC", "SOIC": "SOIC",  # distributors' "SOP-8" is usually SOIC-8; the body size settles it
    "TSSOP": "TSSOP", "HTSSOP": "HTSSOP",  # H: an exposed pad underneath
    "MSOP": "MSOP", "VSSOP": "MSOP", "SSOP": "SSOP",
    "QFN": "QFN", "VQFN": "QFN", "WQFN": "QFN", "UQFN": "QFN", "HVQFN": "QFN", "TQFN": "QFN",
    "DFN": "DFN", "WDFN": "DFN", "UDFN": "DFN", "VDFN": "DFN", "SON": "DFN", "VSON": "DFN", "WSON": "DFN", "USON": "DFN",
    "LQFP": "QFP", "TQFP": "QFP", "QFP": "QFP",
    "SOT-23": "SOT-23", "SOT23": "SOT-23", "SOT-223": "SOT-223", "SOT223": "SOT-223", "SOT-89": "SOT-89",
    "SOT89": "SOT-89", "SC-70": "SC-70", "SC70": "SC-70",
    "SOD-123": "SOD-123", "SOD-123F": "SOD-123F", "SOD-323": "SOD-323", "SOD-523": "SOD-523", "SOD-923": "SOD-923",
    "SMA": "SMA", "SMB": "SMB", "SMC": "SMC", "DPAK": "DPAK", "D2PAK": "D2PAK",
    "TO-92": "TO-92", "TO-220": "TO-220", "DIP": "DIP", "PDIP": "DIP",
}
# JEDEC-style numbers that are another name for one of the above: (family, pins)
NUMBERED = {"SOT-323": ("SC-70", 3), "SOT-353": ("SC-70", 5), "SOT-363": ("SC-70", 6), "SOT-25": ("SOT-23", 5),
            "SOT-26": ("SOT-23", 6), "SOT-23-3": ("SOT-23", 3), "DO-214AC": ("SMA", None),
            "DO-214AA": ("SMB", None), "DO-214AB": ("SMC", None), "TO-252": ("DPAK", None), "TO-263": ("D2PAK", None),
            "SOD-123FL": ("SOD-123F", None)}
DEFAULT_PINS = {"SOT-23": 3, "SC-70": 3}  # "SOT-23" alone is the three-pin one
BODY_FAMILIES = {"QFN", "DFN", "QFP", "SOIC", "TSSOP", "HTSSOP", "MSOP", "SSOP"}  # where the body size tells packages apart
BODY_TOLERANCE = 0.15  # mm


def _family(text: str) -> tuple[str | None, int | None]:
    t = text.upper().replace("_", "-").replace(" ", "-")
    for name in sorted(NUMBERED, key=len, reverse=True):  # longest first: SOT-363 before SOT-3...
        if re.search(rf"(?<![A-Z0-9]){re.escape(name)}(?![0-9A-Z])", t):
            return NUMBERED[name]
    for name in sorted(ALIASES, key=len, reverse=True):
        m = re.search(rf"(?<![A-Z0-9]){re.escape(name)}(?![A-Z])(?:-?(\d+))?", t)
        if m:
            fam = ALIASES[name]
            pins = int(m[1]) if m[1] else None
            if fam in ("SMA", "SMB", "SMC", "DPAK", "D2PAK", "SOD-123", "SOD-123F", "SOD-323", "SOD-523", "SOD-923"):
                pins = None  # a number after these is a variant, not a pin count
            return fam, pins if pins is not None else DEFAULT_PINS.get(fam)
    return None, None


def _body(text: str) -> tuple[float, float] | None:
    for m in re.finditer(r"(?<![A-Za-z])(EP(?=\d))?\(?(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)", text):
        if m[1]:  # the exposed pad's size, not the body's
            continue
        a, b = float(m[2]), float(m[3])
        if 0.5 <= a <= 40 and 0.5 <= b <= 40:
            return tuple(sorted((a, b)))
    return None


def read_package(text: str) -> dict:
    """The facts a distributor's package text states: chip size (imperial code), family, pins, body (mm, short side
    first)."""
    t = text.strip()
    chip = None
    m = re.search(r"(\d{4,5})\s*\(?\s*(\d{4})\s*metric", t, re.I)  # "0603 (1608 Metric)"
    if m and m[1] in CHIP:
        chip = m[1]
    elif (m := re.search(r"(?<!\d)(\d{4})\s*[-_ ]?\s*metric", t, re.I)) and m[1] in METRIC:  # "1608Metric" alone
        chip = METRIC[m[1]]
    elif (m := re.fullmatch(r"(?:[RCL]|LED)?[-_ ]?(\d{4,5})(?:[-_ ].*)?", t, re.I)) and m[1] in CHIP:
        chip = m[1]  # a bare code from a distributor is imperial
    fam, pins = (None, None) if chip else _family(t)
    return {"chip": chip, "family": fam, "pins": pins, "body": _body(t) if fam in BODY_FAMILIES else None}


def read_footprint(name: str) -> dict:
    """The same facts from a KiCad footprint name such as R_0603_1608Metric or QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm."""
    n = name.split(":")[-1]
    m = re.search(r"_(\d{4,5})_(\d{4})Metric", n)
    if m and m[1] in CHIP:
        return {"chip": m[1], "family": None, "pins": None, "body": None}
    fam, pins = _family(n)
    return {"chip": None, "family": fam, "pins": pins, "body": _body(n) if fam in BODY_FAMILIES else None}


def compare(package: str, footprint: str) -> dict:
    """{"result": "match" | "mismatch" | "unknown", "why": [...]} for a distributor's package text and a footprint."""
    p, f = read_package(package), read_footprint(footprint)
    if not (p["chip"] or p["family"]):
        return {"result": "unknown", "why": [f"can't read the package {package!r}"]}
    if not (f["chip"] or f["family"]):
        return {"result": "unknown", "why": [f"can't read the footprint name {footprint!r}"]}
    why = []
    if p["chip"] or f["chip"]:
        if p["chip"] != f["chip"]:
            def size(x):
                return f"{x['chip']} ({CHIP[x['chip']]} metric)" if x["chip"] else (x["family"] or "?")
            why.append(f"the part is {size(p)} but the footprint is {size(f)}")
    else:
        if p["family"] != f["family"]:
            why.append(f"the part is {p['family']} but the footprint is {f['family']}")
        if p["pins"] and f["pins"] and p["pins"] != f["pins"]:
            why.append(f"the part has {p['pins']} pins but the footprint has {f['pins']}")
        if p["body"] and f["body"] and any(abs(a - b) > BODY_TOLERANCE for a, b in zip(p["body"], f["body"])):
            why.append("the part's body is {}x{} mm but the footprint's is {}x{} mm".format(*p["body"], *f["body"]))
    return {"result": "mismatch" if why else "match", "why": why}
