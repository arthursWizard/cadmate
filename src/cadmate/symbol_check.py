"""Review KiCad symbols against the KiCad library conventions (KLC) and the datasheet.

check_symbol applies the symbol rules KiCad's own library is held to (the KLC
checker in kicad-library-utils implements the same ones): pins on the 100 mil
grid, pin lengths, stacking, electrical types, where power pins go, the outline,
centring, and the fields. Messages name the KLC rule. On top of that it flags
things that make a symbol wrong rather than untidy: pins pointing into the body,
overlapping pins, and pin names that run into each other.

compare_with_table checks a symbol against the datasheet's pin table,
compare_with_footprint against its footprint's pads, and compare_symbols
against a reference symbol (for example KiCad's own for the same part).
"""

from __future__ import annotations

import re

from . import footprint as fpmod
from .symbol import GRID, PIN_TYPES, Pin, Symbol

MIL = 0.0254
GROUND = [r"^[ad]*g(rou)*nd(a)*$", r"^[ad]*v(ss)$"]  # KLC S4.2
POSITIVE_POWER = [r"^[ad]*v(aa|cc|dd|bat|in)$", r"^in\+?$"]  # KLC S4.2
POWER_NAMES = [r"^[ad]*g(rou)*nd(a)*$", r"^[ad]*v_?{?(aa|cc|dd|ss|bat|in)}?$"]  # KLC S4.4
NC_NAMES = [r"^nc$", r"^dnc$", r"^n\.c\.$"]  # KLC S4.6
SUGGESTED_TYPES = {  # KLC S4.4, warnings only
    "power_out": [r"^v_?{?out}?"],
    "input": [r"^sdi$", r"^cl(oc)*k(in)*$", r"^~*cs~*$", r"^[av]ref$"],
    "output": [r"^sdo$", r"^cl(oc)*kout$"],
    "bidirectional": [r"^sda$", r"^s*dio$"],
}
CONVERTER_MAX_SIGNALS = 10  # more signal pins than this and a power output is an on-chip regulator
SMALL_SYMBOL = 800 * MIL  # KLC S3.1: smaller symbols may sit 50 mil off centre
CHAR_WIDTH = 1.2  # as symbol_generate


def valid_name(name: str) -> bool:
    """KLC 1.7: letters, digits and _-.+, only."""
    return bool(name) and all(c.isascii() and (c.isalnum() or c in "_-.+,") for c in name)


def _mil(v: float) -> int:
    return round(v / MIL)


def _match(name: str, patterns: list[str]) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


def _label(p: Pin) -> str:
    return f"pin {p.number} ({p.name or 'no name'})" + (f" in unit {p.unit}" if p.unit > 1 else "")


def _text_width(name: str) -> float:
    return len(re.sub(r"[~_^]\{([^}]*)\}", r"\1", name)) * CHAR_WIDTH


def _small(sym: Symbol) -> bool:
    """kicad-library-utils' heuristic for resistors, diodes and the like, which use a 50 mil grid."""
    return len(sym.pins) <= 2 or (len(sym.pins) <= 4 and not sym.rects)


def check_symbol(sym: Symbol) -> dict:
    """Review a symbol. Returns {"ok", "errors", "warnings"}; derived symbols are checked through their parent's drawing."""
    errors: list[str] = []
    warnings: list[str] = []
    _check_fields(sym, errors, warnings)
    if sym.power:
        return {"ok": not errors, "errors": errors, "warnings": warnings}
    small = _small(sym)
    _check_body(sym, small, errors, warnings)
    _check_pins(sym, small, errors, warnings)
    _check_stacks(sym, errors, warnings)
    _check_types(sym, errors, warnings)
    _check_placement(sym, errors, warnings)
    _check_text(sym, warnings)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _check_fields(sym: Symbol, errors: list[str], warnings: list[str]) -> None:
    props = sym.properties
    if not valid_name(sym.name):
        errors.append(f"symbol name {sym.name!r} may only use letters, digits and _-.+, (KLC 1.7)")
    ref = props.get("Reference")
    if ref is None or not ref.value:
        errors.append("no Reference field (KLC S6.2)")
    elif ref.hidden:
        errors.append("Reference field must be visible (KLC S6.2)")
    value = props.get("Value")
    if value is None:
        errors.append("no Value field (KLC S6.2)")
    else:
        if value.value != sym.name:
            errors.append(f"Value {value.value!r} differs from the symbol name {sym.name!r} (KLC S6.2)")
        if value.hidden:
            errors.append("Value field must be visible (KLC S6.2)")
    fp = props.get("Footprint")
    if fp is None:
        errors.append("no Footprint field (KLC S6.2)")
    elif not fp.hidden:
        errors.append("Footprint field must be hidden (KLC S6.2)")
    elif fp.value and ":" not in fp.value:
        warnings.append(f"Footprint {fp.value!r} has no library name; write it as Library:Footprint")
    ds = props.get("Datasheet")
    if ds is None:
        errors.append("no Datasheet field (KLC S6.2)")
    elif not ds.hidden:
        errors.append("Datasheet field must be hidden (KLC S6.2)")
    elif not ds.value or ds.value == "~":
        warnings.append("Datasheet field is empty; KiCad's library always links the datasheet (KLC S6.2)")
    elif not (ds.value.startswith(("http", "www", "ftp")) or ds.value.endswith(".pdf") or ".htm" in ds.value):
        warnings.append(f"Datasheet {ds.value!r} does not look like a URL (KLC S6.2)")
    desc = props.get("Description")
    if desc is None or not desc.value:
        warnings.append("no Description; KiCad's library gives one, e.g. 'USB serial converter, UART, SOIC-16' (KLC S6.2)")
    elif sym.name.lower() in desc.value.lower():
        warnings.append("the Description repeats the symbol name (KLC S6.2)")
    kw = props.get("ki_keywords")
    if kw is None or not kw.value:
        warnings.append("no keywords (KLC S6.2)")
    elif re.findall(r"\.\W|\.$|[,:;?!<>]", kw.value):
        errors.append(f"keywords contain punctuation: {kw.value!r} (KLC S6.2)")
    if not (props.get("ki_fp_filters") and props["ki_fp_filters"].value):
        warnings.append("no footprint filters; give fp_filters (or a default footprint to derive them from) so KiCad offers the right footprints (KLC S5.2)")
    words = " ".join(p.value for k, p in props.items() if k in ("ki_keywords", "Description")).lower()
    tokens = set(re.split(r"[\s,-]+", words))
    keywords = (kw.value.lower() if kw else "")
    if "opamp" in tokens and "operational-amplifier" not in keywords:
        warnings.append("add operational-amplifier to the keywords, KiCad's library pairs it with opamp (KLC S6.2)")
    if "operational" in tokens and "amplifier" in tokens and "opamp" not in keywords.split():
        warnings.append("add opamp to the keywords, KiCad's library pairs it with operational amplifier (KLC S6.2)")
    if "ldo" in tokens and not ("low-dropout" in words and "regulator" in words):
        warnings.append("add low-dropout and regulator to the keywords, KiCad's library pairs them with LDO (KLC S6.2)")
    if "low-dropout" in words and "regulator" in words and "ldo" not in keywords.split():
        warnings.append("add ldo to the keywords, KiCad's library pairs it with low-dropout regulator (KLC S6.2)")


def _check_body(sym: Symbol, small: bool, errors: list[str], warnings: list[str]) -> None:
    for unit in range(1, sym.unit_count + 1):
        pins = sym.pins_of(unit)
        tag = f"unit {unit}" if sym.unit_count > 1 else "symbol"
        body = sym.body(unit)
        if body is not None:
            cx, cy = body.center
            size = max(body.xmax - body.xmin, body.ymax - body.ymin)
            stroke = body.width
        elif pins:
            xs, ys = [p.x for p in pins], [p.y for p in pins]
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            size = max(max(xs) - min(xs), max(ys) - min(ys))
            stroke = None
        else:
            continue
        off = max(abs(_mil(cx)), abs(_mil(cy)))
        if body is None and len(pins) <= 4 and off:
            # A separate power unit is often drawn to sit beside an amplifier unit, as KiCad's opamps do.
            warnings.append(f"{tag} has no body and is centred at ({cx:.3f}, {cy:.3f}) mm, not the origin (KLC S3.1)")
        elif off > 50 or (off == 50 and size > SMALL_SYMBOL) or (0 < off < 50):
            errors.append(f"{tag} is not centred on the origin: centre at ({cx:.3f}, {cy:.3f}) mm (KLC S3.1)")
        elif off == 50:
            warnings.append(f"{tag} is 50 mil off centre; fine for a small symbol (KLC S3.1)")
        if body is not None and len([r for r in sym.rects if r.unit in (unit, 0)]) == 1:
            if abs(stroke - 10 * MIL) > 1e-6:
                (warnings if small else errors).append(f"{tag} outline is {stroke} mm wide; KiCad's library uses 0.254 mm (KLC S3.3)")
            if body.fill != "background":
                (warnings if small else errors).append(f"{tag} body is filled '{body.fill}'; KiCad's library fills it with the background colour (KLC S3.3)")
    off = _mil(sym.pin_names_offset)
    if not sym.hide_pin_names and off:
        if off > 50:
            errors.append(f"pin name offset {sym.pin_names_offset} mm is over 50 mil (KLC S3.6)")
        elif off < 20 and not sym.polylines:  # KiCad's opamps use 5 mil so + and - clear the supply names
            warnings.append(f"pin name offset {sym.pin_names_offset} mm is under 20 mil (KLC S3.6)")
        elif off > 20:
            warnings.append(f"pin name offset {sym.pin_names_offset} mm; KiCad's library uses 20 mil (0.508 mm) unless the drawing needs more (KLC S3.6)")


def _check_pins(sym: Symbol, small: bool, errors: list[str], warnings: list[str]) -> None:
    grid = 50 if small else 100
    off_grid = [p for p in sym.pins if (_mil(p.x) % (50 if p.etype == "no_connect" else grid)) or (_mil(p.y) % (50 if p.etype == "no_connect" else grid))
                or abs(p.x / MIL - _mil(p.x)) > 0.01 or abs(p.y / MIL - _mil(p.y)) > 0.01]
    if off_grid:
        errors.append(f"pins off the {grid} mil grid: {[_label(p) + f' at ({p.x}, {p.y})' for p in off_grid[:8]]} (KLC S4.1)")
    short_err, short_warn = (24, 49) if small else (49, 99)
    for p in sym.pins:
        mil = _mil(p.length)
        if mil == 0:
            continue
        if mil <= short_err:
            errors.append(f"{_label(p)} is {mil} mil long; at least {short_err + 1} mil (KLC S4.1)")
        elif mil <= short_warn:
            warnings.append(f"{_label(p)} is {mil} mil long; KiCad's library uses at least {short_warn + 1} mil (KLC S4.1)")
        elif mil > 300:
            errors.append(f"{_label(p)} is {mil} mil long; at most 300 mil (KLC S4.1)")
        if mil % 50:
            warnings.append(f"{_label(p)} length {mil} mil is not a multiple of 50 mil (KLC S4.1)")
    seen: dict[tuple, Pin] = {}
    for p in sym.pins:
        if not p.number:
            errors.append(f"pin {p.name!r} has no number (KLC S4.1)")
            continue
        for n in p.numbers:
            key = (n, p.unit, p.style)
            if key in seen:
                errors.append(f"pin number {n} is used twice ({seen[key].name} and {p.name}) (KLC S4.1)")
            seen[key] = p
    # KiCad's opamps leave the output unnamed on purpose.
    triangles = {g.unit for g in sym.polylines if len(g.pts) == 4}
    unnamed = [p.number for p in sym.pins if p.name in ("", "~") and p.unit not in triangles]
    if unnamed:
        warnings.append(f"pins without a name: {unnamed[:10]} (KLC S4.1)")
    bad = [p for p in sym.pins if p.etype not in PIN_TYPES]
    if bad:
        errors.append(f"unknown electrical types: {[(_label(p), p.etype) for p in bad[:5]]}")
    # Pins that point into the body instead of away from it.
    for unit in range(1, sym.unit_count + 1):
        body = sym.body(unit)
        if body is None:
            continue
        for p in sym.pins_of(unit):
            if p.hidden or p.length == 0:
                continue
            inside = body.xmin + 1e-6 < p.x < body.xmax - 1e-6 and body.ymin + 1e-6 < p.y < body.ymax - 1e-6
            if inside:
                errors.append(f"{_label(p)} connects inside the body; it points the wrong way")
                continue
            ix, iy = p.inner
            on_edge = (abs(ix - body.xmin) < 1e-3 or abs(ix - body.xmax) < 1e-3) and body.ymin - 1e-3 <= iy <= body.ymax + 1e-3 \
                or (abs(iy - body.ymin) < 1e-3 or abs(iy - body.ymax) < 1e-3) and body.xmin - 1e-3 <= ix <= body.xmax + 1e-3
            if not on_edge:
                warnings.append(f"{_label(p)} does not reach the body outline")


def _stacks(sym: Symbol) -> dict[tuple, list[Pin]]:
    stacks: dict[tuple, list[Pin]] = {}
    units = range(1, sym.unit_count + 1)
    for p in sym.pins:
        for u in ([p.unit] if p.unit else units):
            stacks.setdefault((u, p.style, round(p.x, 3), round(p.y, 3)), []).append(p)
    return stacks


def _number(p: Pin) -> int | None:
    return int(p.number) if p.number.isdigit() else None


def _check_stacks(sym: Symbol, errors: list[str], warnings: list[str]) -> None:
    """KLC S4.3: pins on top of each other share a name, show one pin, and mix types only as allowed."""
    for pins in _stacks(sym).values():
        if len(pins) < 2:
            continue
        where = f"stack at ({pins[0].x}, {pins[0].y})"
        warnings.append(f"{where} ({pins[0].name}) is a legacy stack of {len(pins)} pins; KiCad 10 stacks natively, one pin numbered {'[' + ','.join(p.number for p in pins) + ']'} (KLC S4.3)")
        if len({p.name for p in pins}) > 1:
            errors.append(f"{where} mixes pin names {sorted({p.name for p in pins})}; pins on top of each other must be the same signal (KLC S4.3)")
            continue
        nc = [p.number for p in pins if p.etype == "no_connect"]
        if nc:
            errors.append(f"{where}: no-connect pins {nc} are stacked on other pins (KLC S4.3)")
        visible = [p for p in pins if not p.hidden]
        if len(visible) != 1:
            errors.append(f"{where} ({pins[0].name}) shows {len(visible)} pins; a stack shows exactly one (KLC S4.3)")
        nums = [n for n in (_number(p) for p in pins) if n is not None]
        if visible and nums and _number(visible[0]) is not None and _number(visible[0]) != min(nums):
            warnings.append(f"{where} ({pins[0].name}): the lowest pin number, {min(nums)}, should be the visible one (KLC S4.3)")
        types = [p.etype for p in pins]
        if len(set(types)) == 1 and types[0] not in ("power_in",):
            continue
        n_pass = types.count("passive")
        special = [t for t in types if t in ("power_in", "power_out", "output")]
        if len(set(types)) == 1 and types[0] == "power_in":
            errors.append(f"{where} ({pins[0].name}): the hidden pins under a power input should be passive (KLC S4.3, S4.4)")
        elif n_pass == len(types) - 1 and len(special) == 1:
            if any(p.etype == "passive" and not p.hidden for p in pins):
                errors.append(f"{where} ({pins[0].name}): the passive pins in a power stack must be hidden (KLC S4.3)")
            if any(p.etype != "passive" and p.hidden for p in pins):
                errors.append(f"{where} ({pins[0].name}): the {special[0]} pin in the stack must be the visible one (KLC S4.3)")
        elif len(set(types)) > 1:
            errors.append(f"{where} ({pins[0].name}) mixes types {sorted(set(types))}; only one power or output pin over hidden passives is allowed (KLC S4.3)")


def _check_types(sym: Symbol, errors: list[str], warnings: list[str]) -> None:
    for pins in _stacks(sym).values():
        visible = [p for p in pins if not p.hidden]
        p = (visible or pins)[0]
        if _match(p.name.lower(), POWER_NAMES) and p.etype != "power_in":
            errors.append(f"{_label(p)} is a power pin by its name but has type {p.etype}; use power_in (KLC S4.4)")
    for p in sym.pins:
        if re.search(r"~\{.+\}", p.name) and p.shape == "inverted":
            errors.append(f"{_label(p)} is inverted twice: an overbar on the name and an inverted pin shape (KLC S4.4)")
        for etype, patterns in SUGGESTED_TYPES.items():
            if _match(p.name.lower(), patterns):
                if p.etype != etype and not p.hidden:
                    warnings.append(f"{_label(p)} has type {p.etype}; its name suggests {etype} (KLC S4.4)")
                break
        if _match(p.name.lower(), NC_NAMES) or p.etype == "no_connect":
            if p.etype != "no_connect":
                errors.append(f"{_label(p)} is named as not connected but has type {p.etype}; use no_connect (KLC S4.6)")
            if not p.hidden:
                warnings.append(f"{_label(p)} is a no-connect pin; KiCad's library hides them (KLC S4.6)")
        if p.etype == "power_in" and p.hidden:
            errors.append(f"{_label(p)} is a hidden power input; power pins must be visible outside power symbols (KLC S4.6)")


def is_converter(sym: Symbol) -> bool:
    """kicad-library-utils treats any part with a power output as a power converter; KiCad's library
    then files exceptions for MCUs with an on-chip regulator. Only parts with few signals count here."""
    power_out = [p for p in sym.pins if p.etype == "power_out" and not p.hidden]
    signals = [p for p in sym.pins if not p.hidden and p.etype not in ("power_in", "power_out")]
    return bool(power_out) and len(signals) <= CONVERTER_MAX_SIGNALS


def s42_exceptions_needed(sym: Symbol) -> dict[str, str]:
    """Pins kicad-library-utils flags under KLC S4.2 that are placed so on purpose, by pin name, with
    the reason; written as KLC_S4.2_<pin> fields, the way KiCad's own library records exceptions."""
    power_out = [p for p in sym.pins if p.etype == "power_out" and not p.hidden]
    if not power_out or is_converter(sym):
        return {}
    out = {}
    for p in sym.pins:
        if not p.hidden and p.etype == "power_in" and _match(p.name, POSITIVE_POWER) and p.side != "left":
            out[p.name] = "Not a power converter: its power output is an on-chip regulator, so the supplies stay on top"
    for p in power_out:
        if p.side != "right":
            out[p.name] = "On-chip regulator output, placed with the supplies it feeds"
    return out


def _check_placement(sym: Symbol, errors: list[str], warnings: list[str]) -> None:
    """KLC S4.2: grounds at the bottom, supplies at the top, a converter's input left and output right."""
    power_out = [p for p in sym.pins if p.etype == "power_out" and not p.hidden]
    converter = is_converter(sym)
    excepted = {k[len("KLC_S4.2_"):] for k in sym.properties if k.startswith("KLC_S4.2_")}
    for p in sym.pins:
        if p.hidden or p.name in excepted:
            continue
        name = p.name.lower()
        if _match(name, GROUND) and p.side != "bottom":
            warnings.append(f"{_label(p)} is a ground pin on the {p.side}; KiCad's library puts grounds at the bottom (KLC S4.2)")
        if p.etype == "power_in" and _match(p.name, POSITIVE_POWER):
            want = "left" if converter else "top"
            if p.side != want:
                why = "a power converter's input goes on the left" if converter else "positive supplies go at the top"
                warnings.append(f"{_label(p)} is on the {p.side}; {why} (KLC S4.2)")
    for p in power_out:
        if p.side != "right" and p.name not in excepted:
            warnings.append(f"{_label(p)} is a power output on the {p.side}; power outputs go on the right (KLC S4.2)")


def _check_text(sym: Symbol, warnings: list[str]) -> None:
    """Pin names that would run into each other across the body, or out of it."""
    if sym.hide_pin_names or sym.pin_names_offset == 0:
        return
    off = sym.pin_names_offset
    for unit in range(1, sym.unit_count + 1):
        body = sym.body(unit)
        if body is None:
            continue
        pins = [p for p in sym.pins_of(unit) if not p.hidden and p.name and p.name != "~"]
        left = {round(p.y, 3): p for p in pins if p.side == "left"}
        right = {round(p.y, 3): p for p in pins if p.side == "right"}
        width = body.xmax - body.xmin
        for y in sorted(set(left) & set(right)):
            need = _text_width(left[y].name) + _text_width(right[y].name) + 2 * off + 0.5
            if need > width:
                warnings.append(f"names of pins {left[y].number} ({left[y].name}) and {right[y].number} ({right[y].name}) may overlap across the body")
        for p in pins:
            if p.side in ("left", "right") and _text_width(p.name) + off > width:
                warnings.append(f"name of {_label(p)} is wider than the body")
        tops = [p for p in pins if p.side == "top"]
        bottoms = [p for p in pins if p.side == "bottom"]
        height = body.ymax - body.ymin
        for p in tops + bottoms:
            if _text_width(p.name) + off > height:
                warnings.append(f"name of {_label(p)} is taller than the body")
        # Top and bottom names run vertically into the body; side rows must stay below them.
        sides = [p for p in pins if p.side in ("left", "right")]
        for p in tops:
            reach = body.ymax - off - _text_width(p.name)
            hit = [q for q in sides if q.y > reach - 0.6 and _hits_x(q, p.x, body, off)]
            if hit:
                warnings.append(f"name of top {_label(p)} may run into the name of {_label(hit[0])}")
        for p in bottoms:
            reach = body.ymin + off + _text_width(p.name)
            hit = [q for q in sides if q.y < reach + 0.6 and _hits_x(q, p.x, body, off)]
            if hit:
                warnings.append(f"name of bottom {_label(p)} may run into the name of {_label(hit[0])}")


def _hits_x(q: Pin, x: float, body, off: float) -> bool:
    w = _text_width(q.name)
    if q.side == "left":
        return body.xmin + off - 0.6 <= x <= body.xmin + off + w + 0.6
    return body.xmax - off - w - 0.6 <= x <= body.xmax - off + 0.6


# --- comparisons ---------------------------------------------------------------------------------


def _norm(name: str) -> str:
    """Compare names the way a reader does: overbar spellings and case don't matter."""
    n = name.strip()
    n = re.sub(r"^[/!]|[#*]$", "", n)
    n = re.sub(r"~\{([^}]*)\}", r"\1", n).replace("~", "")
    return n.upper()


def _same_name(datasheet: str, name: str) -> bool:
    d = _norm(datasheet).replace(" ", "_")
    if d == _norm(name):
        return True
    # Opamp units show + and - and leave the output unnamed, as KiCad's do.
    if name in ("", "~"):
        return True
    return (name == "+" and ("+" in d or d in ("INP", "IN_P"))) or (name == "-" and ("-" in d or d in ("INN", "IN_N")))


def compare_with_table(sym: Symbol, table: list[dict]) -> dict:
    """Check a symbol against the datasheet pin table: every pin present, with the datasheet's name and type."""
    from .symbol_generate import _pin_type  # the same type shorthand the generator accepts

    errors: list[str] = []
    warnings: list[str] = []
    by_number: dict[str, list[Pin]] = {}
    for p in sym.pins:
        for n in p.numbers:
            by_number.setdefault(n, []).append(p)
    wanted = set()
    for row in table or []:
        if not isinstance(row, dict) or row.get("number") in (None, ""):
            continue
        number = str(row["number"]).strip()
        wanted.add(number)
        found = by_number.get(number)
        if not found:
            errors.append(f"datasheet pin {number} ({row.get('name', '')}) is missing from the symbol")
            continue
        p = found[0]
        if row.get("name") is not None and not _same_name(str(row["name"]), p.name):
            errors.append(f"pin {number} is {p.name!r} in the symbol but {row['name']!r} in the datasheet")
        try:
            etype = _pin_type(row.get("type"), str(row.get("name") or ""), number) if row.get("type") or row.get("name") else None
        except ValueError:
            etype = None
        if etype and etype != p.etype and not (p.hidden and p.etype == "passive" and etype == "power_in"):
            warnings.append(f"pin {number} ({p.name}) has type {p.etype}; the datasheet table says {etype}")
    extra = sorted(set(by_number) - wanted, key=_sort_key)
    if table and extra:
        errors.append(f"symbol pins not in the datasheet table: {extra[:15]}")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _sort_key(n: str):
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", n)
    return (m.group(1), int(m.group(2))) if m else (n, 0)


def compare_with_footprint(sym: Symbol, fp: fpmod.Footprint) -> dict:
    """Pins and pads must match by number, or KiCad cannot connect them on the board."""
    pads = {p.number for p in fp.pads if p.number and p.kind != "np_thru_hole"}
    pins = {n for p in sym.pins for n in p.numbers}
    errors, warnings = [], []
    no_pin = sorted(pads - pins, key=_sort_key)
    no_pad = sorted(pins - pads, key=_sort_key)
    if no_pad:
        errors.append(f"symbol pins with no pad on {fp.name}: {no_pad[:15]}; KiCad reports these when updating the board")
    if no_pin:
        errors.append(f"pads of {fp.name} with no symbol pin: {no_pin[:15]}; they would stay unconnected (add the pin, hidden if NC)")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "pads": len(pads), "pins": len(pins)}


def compare_symbols(candidate: Symbol, reference: Symbol) -> dict:
    """Pin by pin differences between two symbols, matched by pin number."""
    def index(sym: Symbol) -> dict[str, Pin]:
        # A legacy stack (hidden passives under a visible pin) reads as the visible pin, so it
        # compares equal to KiCad 10's native stack of the same pads.
        shown = {(p.unit, round(p.x, 3), round(p.y, 3), p.name): p for p in sym.pins if not p.hidden}
        out: dict[str, Pin] = {}
        for p in sym.pins:
            lead = shown.get((p.unit, round(p.x, 3), round(p.y, 3), p.name), p) if p.hidden else p
            for n in p.numbers:
                out.setdefault(n, lead)
        return out

    a, b = index(candidate), index(reference)
    diffs = []
    for n in sorted(set(a) & set(b), key=_sort_key):
        pa, pb = a[n], b[n]
        d = {}
        if _norm(pa.name) != _norm(pb.name):
            d["name"] = [pa.name, pb.name]
        if pa.etype != pb.etype:
            d["type"] = [pa.etype, pb.etype]
        if pa.side != pb.side and not (pa.hidden and pb.hidden):
            d["side"] = [pa.side, pb.side]
        if pa.hidden != pb.hidden:
            d["hidden"] = [pa.hidden, pb.hidden]
        if pa.unit != pb.unit and not (candidate.unit_count == reference.unit_count == 1):
            d["unit"] = [pa.unit, pb.unit]
        if d:
            diffs.append({"number": n, **d})
    fields = {}
    for key in ("Reference", "Footprint"):
        va, vb = candidate.prop(key), reference.prop(key)
        if (va or "") != (vb or ""):
            fields[key] = [va, vb]
    electrical = [d for d in diffs if "name" in d or "type" in d or "unit" in d]
    return {
        "same_pins": not diffs and set(a) == set(b),
        "same_connections": not electrical and set(a) == set(b),
        "only_in_candidate": sorted(set(a) - set(b), key=_sort_key),
        "only_in_reference": sorted(set(b) - set(a), key=_sort_key),
        "differences": diffs,
        "fields": fields,
        "units": [candidate.unit_count, reference.unit_count],
        "bodies": [[round(r.xmax - r.xmin, 3), round(r.ymax - r.ymin, 3)] for r in (candidate.body(1), reference.body(1)) if r],
    }
