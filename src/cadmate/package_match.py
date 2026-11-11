"""Does the part being bought fit the footprint on the board?

A distributor lists a part's package as text: "0805", "SOT-23-5", "QFN-32-EP(5x5)" (LCSC), "SOP-8_L4.9-W3.9-P1.27"
(LCSC's long form), "14-SOIC (0.154", 3.90mm Width)" or "8-TSSOP, 8-MSOP" (DigiKey). The board has a footprint name:
"R_0603_1608Metric", "SOT-23-6", "QFN-32-1EP_4x4mm_P0.4mm_EP2.7x2.7mm". Both are read into the same few facts (chip
size, family, pin count, body size or width, pitch, exposed pad) and compared.

Only facts both sides state are compared, so "match" means nothing that was stated disagrees, and text that can't be
read gives "unknown", never a guess. Two readings go further than the text, on purpose: a bare "SOT-23" or "SC-70" is
the three-pin one (that's what distributors and KiCad both mean by it), and a part listed with an exposed pad needs a
footprint with one (a footprint with a pad the part lacks is only a soldering question, so that isn't flagged). When
the text lists alternatives ("8-TSSOP, 8-MSOP"), any one that fits is a match.
"""

from __future__ import annotations

import re

# chip sizes: imperial code -> metric code
CHIP = {"01005": "0402", "0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216",
        "1210": "3225", "1812": "4532", "2010": "5025", "2512": "6332"}
METRIC = {m: i for i, m in CHIP.items()}

# names for the same package: name -> (family, exposed pad); longest names are tried first
ALIASES = {
    "SO": ("SOIC", False), "SOP": ("SOIC", False), "SOIC": ("SOIC", False),  # distributors' SOP-8 is SOIC-8's body;
    "ESOP": ("SOIC", True), "HSOP": ("SOIC", True),                         # the width settles it (208 mil is wider)
    "TSSOP": ("TSSOP", False), "HTSSOP": ("TSSOP", True),
    "MSOP": ("MSOP", False), "VSSOP": ("MSOP", False), "HVSSOP": ("MSOP", True), "SSOP": ("SSOP", False),
    "QSOP": ("SSOP", False),  # a 0.635 mm pitch SSOP; KiCad names them SSOP-…_P0.635mm
    "QFN": ("QFN", False), "VQFN": ("QFN", False), "WQFN": ("QFN", False), "UQFN": ("QFN", False),
    "HVQFN": ("QFN", False), "TQFN": ("QFN", False), "VFQFN": ("QFN", False), "UFQFN": ("QFN", False),
    "UFQFPN": ("QFN", False),
    "DFN": ("DFN", False), "WDFN": ("DFN", False), "UDFN": ("DFN", False), "VDFN": ("DFN", False), "SON": ("DFN", False),
    "VSON": ("DFN", False), "TDFN": ("DFN", False), "POWERTDFN": ("DFN", False), "WSON": ("DFN", False), "USON": ("DFN", False),
    "LQFP": ("QFP", False), "TQFP": ("QFP", False), "QFP": ("QFP", False),
    "SOT-23": ("SOT-23", False), "SOT23": ("SOT-23", False), "TSOT-23": ("SOT-23", False),  # TSOT: same pads, thinner
    "TSOT23": ("SOT-23", False), "SOT-223": ("SOT-223", False), "SOT223": ("SOT-223", False),
    "SOT-89": ("SOT-89", False), "SOT89": ("SOT-89", False), "SOT-563": ("SOT-563", False), "SC-70": ("SC-70", False),
    "SC70": ("SC-70", False),
    "SOD-123": ("SOD-123", False), "SOD-123F": ("SOD-123F", False), "SOD-323": ("SOD-323", False),
    "SOD-523": ("SOD-523", False), "SOD-923": ("SOD-923", False),
    "SMA": ("SMA", False), "SMAF": ("SMAF", False), "SMB": ("SMB", False), "SMC": ("SMC", False),
    "DPAK": ("TO-252", False), "D2PAK": ("TO-263", False), "TO-252": ("TO-252", False), "TO-263": ("TO-263", False),
    "TO-92": ("TO-92", False), "TO-220": ("TO-220", False), "DIP": ("DIP", False), "PDIP": ("DIP", False),
}
# numbers that are another name for a package with a set pin count: name -> (family, pins)
NUMBERED = {"SOT-323": ("SC-70", 3), "SOT-353": ("SC-70", 5), "SOT-363": ("SC-70", 6), "SOT-25": ("SOT-23", 5),
            "SOT-26": ("SOT-23", 6), "SOT-753": ("SOT-23", 5), "SC-74A": ("SOT-23", 5), "SOT-457": ("SOT-23", 6),
            "SC-74": ("SOT-23", 6), "DO-214AC": ("SMA", None), "DO-214AA": ("SMB", None), "DO-214AB": ("SMC", None),
            "SOD-123FL": ("SOD-123F", None)}
DEFAULT_PINS = {"SOT-23": 3, "SC-70": 3}
NO_PIN_COUNT = {"SMA", "SMAF", "SMB", "SMC", "SOD-123", "SOD-123F", "SOD-323", "SOD-523", "SOD-923"}  # a number after
# these names is a variant, not a pin count
TAB_FAMILIES = {"TO-252", "TO-263", "SOT-223", "SOT-89"}  # SMD tabs: sources count the tab as a pin, or don't
# (KiCad's TO-220-N counts real leads, as distributors do)
BODY_FAMILIES = {"QFN", "DFN", "QFP", "SOIC", "TSSOP", "MSOP", "SSOP"}  # where size tells packages apart
BODY_TOLERANCE = 0.15  # mm
WIDTH_TOLERANCE = 0.25  # mm, for widths given in mil (150 mil is the 3.9 mm body)
MSOP_WIDTH = 3.0  # mm: every MSOP/VSSOP is a 3 mm body, which KiCad also names TSSOP-N_3x3mm (same land pattern)
PITCH_TOLERANCE = 0.01  # mm: 0.64 is 0.635 rounded, while 0.635 and 0.65 are different parts


def _family(text: str) -> tuple[str | None, int | None, bool]:
    """(family, pins, exposed pad) from a name, with DigiKey's leading pin count ("14-SOIC") as well."""
    t = text.upper().replace("_", "-").replace(" ", "-")
    for name in sorted(NUMBERED, key=len, reverse=True):
        if re.search(rf"(?<![A-Z0-9]){re.escape(name)}(?![0-9A-Z])", t):
            fam, pins = NUMBERED[name]
            return fam, pins, False
    for name in sorted(ALIASES, key=len, reverse=True):
        m = re.search(rf"(?:(?<![A-Z0-9.])(\d+)-)?(?<![A-Z0-9]){re.escape(name)}(?![A-Z])(?:-?(\d+))?", t)
        if m:
            fam, ep = ALIASES[name]
            pins = int(m[2] or m[1]) if (m[1] or m[2]) and fam not in NO_PIN_COUNT else None
            ep = ep or bool(re.search(r"(?<![A-Z])(\d*EP|EPAD|E-PAD|EXPOSED-PAD|POWERPAD|THERMAL-PAD)(?![A-Z])", t))
            return fam, pins if pins is not None else DEFAULT_PINS.get(fam), ep
    return None, None, False


def _size(text: str) -> dict:
    """Body (both sides, short first) or width alone, and pitch, from any of the ways they're written."""
    out: dict = {}
    for m in re.finditer(r"(?<![A-Za-z])(EP(?=\d))?\(?(\d+(?:\.\d+)?)\s*(?:mm)?\s*[xX×*]\s*(\d+(?:\.\d+)?)", text):
        if not m[1]:  # an exposed pad's size, not the body's
            a, b = float(m[2]), float(m[3])
            if 0.5 <= a <= 40 and 0.5 <= b <= 40:
                out["body"] = tuple(sorted((a, b)))
                out["named_width"] = a  # KiCad names an SO body width first: TSSOP-8_4.4x3mm
                break
    if "body" not in out and (m := re.search(r"(?<![A-Za-z])L(\d+(?:\.\d+)?)-W(\d+(?:\.\d+)?)", text, re.I)):
        out["body"] = tuple(sorted((float(m[1]), float(m[2]))))  # LCSC: "L4.9-W3.9"
        out["named_width"] = float(m[2])
    if "body" not in out:
        if m := re.search(r"(\d+(?:\.\d+)?)\s*mm\s*width", text, re.I):  # DigiKey: "3.90mm Width"
            out["width"], out["width_tol"] = float(m[1]), BODY_TOLERANCE
        elif m := re.search(r"(?<![\d.])(\d{3})\s*mil(?![a-z])", text, re.I):  # "SOP-8_208mil"
            out["width"], out["width_tol"] = int(m[1]) * 0.0254, WIDTH_TOLERANCE
    if m := re.search(r"(?<![A-Za-z])P(\d+(?:\.\d+)?)(?:mm)?(?![\d.])", text) or \
            re.search(r"(\d+(?:\.\d+)?)\s*mm\s*pitch", text, re.I):
        out["pitch"] = float(m[1])
    return out


def _chip(t: str) -> str | None:
    if (m := re.search(r"(\d{4,5})\s*\(\s*(\d{4})\s*(?:metric)?\s*\)", t, re.I)) and CHIP.get(m[1]) == m[2]:
        return m[1]  # "0603 (1608 Metric)", "0603(1608)"
    if (m := re.search(r"(?<!\d)(\d{4})\s*[-_ ]?\s*metric", t, re.I)) and m[1] in METRIC:
        return METRIC[m[1]]  # "1608Metric" alone
    if (m := re.fullmatch(r"(?:[RCL]|LED|SMD)?[-_ ,]?(\d{4,5})(?:[-_ ].*)?", t, re.I)) and m[1] in CHIP:
        return m[1]  # a bare code from a distributor is imperial
    return None


def _split(text: str) -> list[str]:
    """Alternatives in one listing ("8-TSSOP, 8-MSOP"), split on commas outside brackets."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        if ch in ",;" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [p.strip() for p in parts + [cur] if p.strip()]


def _read(text: str) -> dict:
    chip = _chip(text)
    if chip:
        return {"chip": chip, "family": None, "pins": None, "ep": False}
    fam, pins, ep = _family(text)
    out = {"chip": None, "family": fam, "pins": pins, "ep": ep, **(_size(text) if fam in BODY_FAMILIES else {})}
    if "pitch" not in out and re.search(r"(?<![A-Z])QSOP", text.upper()):
        out["pitch"] = 0.635
    return out


def read_package(text: str) -> list[dict]:
    """The facts a distributor's package text states, one dict per alternative it lists."""
    alts = [_read(a) for a in _split(text.strip())]
    whole = _read(text.strip())  # a size or pitch given once for all of them, e.g. "SOIC-8, 3.90mm Width"
    for a in alts:
        for k in ("body", "width", "width_tol", "pitch"):
            if k not in a and k in whole and a["family"] in BODY_FAMILIES:
                a[k] = whole[k]
    return alts


def read_footprint(name: str) -> dict:
    """The same facts from a KiCad footprint name such as R_0603_1608Metric or QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm."""
    n = name.split(":")[-1]
    m = re.search(r"_(\d{4,5})_(\d{4})Metric", n)
    if m and m[1] in CHIP:
        return {"chip": m[1], "family": None, "pins": None, "ep": False}
    return _read(n)


def _differences(p: dict, f: dict) -> list[str]:
    if p["chip"] or f["chip"]:
        if p["chip"] == f["chip"]:
            return []
        def size(x):
            return f"{x['chip']} ({CHIP[x['chip']]} metric)" if x["chip"] else x["family"]
        return [f"the part is {size(p)} but the footprint is {size(f)}"]
    why = []
    if {p["family"], f["family"]} == {"MSOP", "TSSOP"}:
        why += _msop_as_tssop(p, f)
    elif p["family"] != f["family"]:
        why.append(f"the part is {p['family']} but the footprint is {f['family']}")
    if p["pins"] and f["pins"] and p["pins"] != f["pins"]:
        if not (p["family"] in TAB_FAMILIES and abs(p["pins"] - f["pins"]) == 1):  # TO-252-3 is KiCad's TO-252-2
            why.append(f"the part has {p['pins']} pins but the footprint has {f['pins']}")
    if p["ep"] and not f["ep"]:
        why.append("the part has an exposed pad underneath but the footprint has none for it")
    if p.get("body") and f.get("body") and any(abs(a - b) > BODY_TOLERANCE for a, b in zip(p["body"], f["body"])):
        why.append("the part's body is {:g}x{:g} mm but the footprint's is {:g}x{:g} mm".format(*p["body"], *f["body"]))
    elif p.get("width") and f.get("body") and abs(p["width"] - f.get("named_width", f["body"][0])) > p["width_tol"]:
        why.append(f"the part's body is {p['width']:.2f} mm wide but the footprint's is "
                   f"{f.get('named_width', f['body'][0]):g} mm")
    if p.get("pitch") and f.get("pitch") and abs(p["pitch"] - f["pitch"]) > PITCH_TOLERANCE:
        why.append(f"the part's pitch is {p['pitch']:g} mm but the footprint's is {f['pitch']:g} mm")
    return why


def _msop_as_tssop(p: dict, f: dict) -> list[str]:
    """MSOP and TSSOP are one land pattern when the TSSOP side is the 3 mm body (KiCad's TSSOP-8_3x3mm_P0.65mm is an
    MSOP-8). TSSOP's usual 4.4 mm body is not, and a TSSOP whose width isn't stated stays a mismatch: that's the guess."""
    t = p if p["family"] == "TSSOP" else f
    width = t.get("named_width", t.get("width"))
    if width is not None and abs(width - MSOP_WIDTH) <= t.get("width_tol", BODY_TOLERANCE):
        return []
    what = f"the {width:g} mm TSSOP body" if width is not None else "TSSOP of unstated width (usually 4.4 mm)"
    if t is f:
        return [f"the part is MSOP (3 mm body) but the footprint is {what}"]
    return [f"the part is {what} but the footprint is MSOP (3 mm body)"]


def compare(package: str, footprint: str) -> dict:
    """{"result": "match" | "mismatch" | "unknown", "why": [...]} for a distributor's package text and a footprint."""
    f = read_footprint(footprint)
    alts = [a for a in read_package(package) if a["chip"] or a["family"]]
    if not alts:
        return {"result": "unknown", "why": [f"can't read the package {package!r}"]}
    if not (f["chip"] or f["family"]):
        return {"result": "unknown", "why": [f"can't read the footprint name {footprint!r}"]}
    found = [_differences(a, f) for a in alts]
    if any(not w for w in found):
        return {"result": "match", "why": []}
    return {"result": "mismatch", "why": found[0]}
