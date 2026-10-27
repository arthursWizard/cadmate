"""Generate KiCad schematic symbols from a datasheet's pin table.

The agent reads the pin table (number, name, type, and optionally a side and a
group); everything from there on is deterministic and follows the KiCad library
conventions (KLC S3, S4, S6): pins on the 100 mil grid, inputs on the left,
outputs on the right, positive supplies on top and grounds at the bottom (a
regulator's input on the left and output on the right), power pins of the same
name stacked under one visible pin, no-connect pins hidden, the body a
background-filled rectangle centred on the origin, and the Footprint, Datasheet
and Description fields filled in and hidden. The file is written in KiCad 10's
format, as its symbol editor writes it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import sexpr, symbol, symbol_check
from .symbol import GRID, PIN_SHAPES, PIN_TYPES, Pin, Polyline, Rect, Symbol

FONT = 1.27  # 50 mil, KLC S3.2
CHAR_WIDTH = 1.2  # mm per character of KiCad's stroke font at 1.27 mm, measured on kicad-cli renders, plus a little
PIN_NAME_OFFSET = 0.508  # 20 mil, KLC S3.6
OUTLINE_WIDTH = 0.254  # 10 mil, KLC S3.3
GAP = 2.54  # between the left and right pin names
NAME_CLEARANCE = 0.635  # between the end of a top or bottom pin name and the side names under it
MIN_HALF_WIDTH = 5.08

# Datasheet shorthand -> KiCad electrical type.
TYPE_ALIASES = {
    "i": "input", "in": "input", "o": "output", "out": "output", "io": "bidirectional", "i/o": "bidirectional",
    "bidir": "bidirectional", "b": "bidirectional", "tri": "tri_state", "tristate": "tri_state", "3s": "tri_state",
    "p": "power_in", "pwr": "power_in", "power": "power_in", "supply": "power_in", "s": "power_in", "g": "power_in",
    "gnd": "power_in", "ground": "power_in", "po": "power_out", "power_output": "power_out",
    "od": "open_collector", "open_drain": "open_collector", "oc": "open_collector", "open_source": "open_emitter",
    "nc": "no_connect", "dnc": "no_connect", "a": "passive", "analog": "passive", "ai": "input", "ao": "output",
    "passive": "passive",
}
GROUND = re.compile(r"^([ad]*g(rou)*nd(a)*|[adp]?gnd\w*|v(ss|ee)\w*|v-|ep|pad)$", re.IGNORECASE)
POSITIVE = re.compile(r"^([ad]*v(aa|cc|dd|bat|in)\w*|v\+|vs|vs\+|vio|vddio|iovdd|\w*vdd\w*|\w*vcc\w*)$", re.IGNORECASE)
POWER_OUT = re.compile(r"^v_?\{?out\}?\w*$", re.IGNORECASE)
NO_CONNECT = re.compile(r"^(nc|dnc|n\.c\.|n/c)$", re.IGNORECASE)
LEFT_TYPES = {"input"}
RIGHT_TYPES = {"output", "tri_state", "open_collector", "open_emitter", "power_out"}

FIELDS = {
    "description": "A one-line summary for the Description field, like KiCad's: function, then package, e.g. 'USB serial converter, UART, SOIC-16'. Leave the part name out.",
    "keywords": "Search words for ki_keywords, space separated, none already in the description",
    "reference": "Reference prefix, U for ICs (default), J for connectors, Q, D, ...",
    "footprint": "Default footprint as Library:Name, e.g. Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
    "datasheet": "Datasheet URL",
    "fp_filters": "Footprint filter patterns; derived from the footprint when left out",
}
PIN_FIELDS = {
    "number": "Pin (pad) number as the footprint has it: 1, 2, ... or A1, B2 for BGAs",
    "name": "Pin name from the datasheet; active-low as ~{RST} (cadmate converts /RST, !RST and RST#)",
    "type": f"Electrical type: {', '.join(PIN_TYPES)}; datasheet shorthand like I, O, I/O, P, G, OD and NC works. Optional for power, ground and NC pins named as such.",
    "side": "Optional: left, right, top or bottom; worked out from the type when left out",
    "group": "Optional: pins with the same group sit together on one side, with a gap before the next group (e.g. USB, QSPI, GPIO)",
    "shape": f"Optional graphic style: {', '.join(PIN_SHAPES)} (default line). Use inverted only when the name has no ~{{}} overbar.",
    "unit": "Optional unit number from 1, for parts split into units (e.g. one per amplifier, plus one for power)",
    "hidden": "Optional; only for no-connect pins, which are hidden anyway",
    "alternates": "Optional alternate functions (KiCad pin alternates): names, or {name, type, shape}",
}


@dataclass
class _Slot:
    """One visible pin position: the pin shown there and any stacked under it."""

    pins: list[Pin]
    group: str | None = None
    order: int = 0

    @property
    def lead(self) -> Pin:
        return self.pins[0]


@dataclass
class _Unit:
    number: int
    pins: list[Pin] = field(default_factory=list)
    rect: Rect | None = None
    outline: Polyline | None = None  # the opamp triangle, drawn instead of a rectangle
    box_hint: Rect | None = None  # a unit drawn without a body: the area its pins frame, for placing the fields

    @property
    def box(self) -> Rect:
        if self.rect is not None:
            return self.rect
        if self.box_hint is not None:
            return self.box_hint
        xs, ys = [x for x, _ in self.outline.pts], [y for _, y in self.outline.pts]
        return Rect(min(xs), min(ys), max(xs), max(ys))


def generate(
    name: str,
    pins: list[dict],
    reference: str = "U",
    footprint: str | None = None,
    datasheet: str | None = None,
    description: str | None = None,
    keywords: str | None = None,
    fp_filters: list[str] | str | None = None,
    pin_length: float | None = None,
    stack_power_pins: bool = True,
    unit_shapes: dict | None = None,
) -> dict:
    """Build a symbol. Returns {"ok", "name", "text", "report", "errors", "warnings", "summary"}; text is set only when ok."""
    warnings: list[str] = []
    try:
        table = [_read_pin(p, i, warnings) for i, p in enumerate(pins or [])]
    except (ValueError, TypeError, KeyError) as e:
        return _fail([str(e)], warnings)
    if not table:
        return _fail(["no pins"], warnings)
    errors = _table_errors(table, name, reference)
    if errors:
        return _fail(errors, warnings)

    length = pin_length if pin_length is not None else _pin_length(table, stack_power_pins)
    if length < GRID or abs(length / 1.27 - round(length / 1.27)) > 1e-6:
        return _fail([f"pin_length {length} mm must be at least {GRID} mm and a multiple of 1.27 mm (50 mil)"], warnings)

    shapes = {int(k): str(v) for k, v in (unit_shapes or {}).items()}
    bad = {k: v for k, v in shapes.items() if v not in ("rectangle", "opamp")}
    if bad:
        return _fail([f"unit_shapes must be rectangle or opamp, got {bad}"], warnings)
    rows = [(p.number, p.etype, p) for p in table]  # before stacking renumbers the stacks
    opamps = "opamp" in shapes.values()
    # KiCad's opamps set the names 5 mil in, so + and - clear the supply pin names beside them.
    sym = Symbol(name=name, pin_names_offset=OPAMP_NAME_OFFSET if opamps else PIN_NAME_OFFSET)
    report: dict = {"pin_length": length, "units": {}}
    units = sorted({p.unit for p in table})
    laid_out: list[_Unit] = []
    for u in units:
        pins_u = [p for p in table if p.unit == u]
        try:
            if shapes.get(u) == "opamp":
                unit = _layout_opamp(u, pins_u, report, warnings)
            elif opamps and shapes.get(u) != "rectangle" and _supplies_only(pins_u):
                unit = _layout_supply_unit(u, pins_u, report)
            else:
                unit = _layout(u, pins_u, length, stack_power_pins, report, warnings)
        except ValueError as e:
            return _fail([str(e)], warnings)
        laid_out.append(unit)
        sym.pins += unit.pins
        if unit.rect is not None:
            sym.rects.append(unit.rect)
        if unit.outline is not None:
            sym.polylines.append(unit.outline)

    filters = _filters(fp_filters, footprint)
    sym.properties = _fields(sym, reference, footprint, datasheet, description, keywords, filters, laid_out, length)
    if len(units) > 1 and not _interchangeable(laid_out):
        sym.units_locked = True
    for pin_name, reason in symbol_check.s42_exceptions_needed(sym).items():
        sym.properties[f"KLC_S4.2_{pin_name}"] = symbol.Property(reason, 0, 0, True, private=True)
        report.setdefault("klc_exceptions", []).append(f"S4.2 {pin_name}: {reason}")
    report["fp_filters"] = filters

    text = to_kicad_sym(sym)
    parsed = symbol.parse_symbol(text)  # never hand out a symbol we cannot read back
    review = symbol_check.check_symbol(parsed)
    # Against the table as read (overbars converted, opamp inputs named + and -).
    review_vs = symbol_check.compare_with_table(parsed, [{"number": n, "name": p.name, "type": t} for n, t, p in rows])
    errors = review["errors"] + review_vs["errors"]
    warnings += review["warnings"] + review_vs["warnings"]
    return {
        "ok": not errors,
        "name": name,
        "text": text if not errors else None,
        "report": report,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "pins": len(parsed.pins),
            "visible_pins": sum(not p.hidden for p in parsed.pins),
            "units": parsed.unit_count,
            "bodies": [{"unit": u.number, "shape": "rectangle" if u.rect else "opamp" if u.outline else "none", "width": round(u.box.xmax - u.box.xmin, 4),
                        "height": round(u.box.ymax - u.box.ymin, 4)} for u in laid_out],
        },
    }


def _fail(errors: list[str], warnings: list[str] | None = None) -> dict:
    return {"ok": False, "name": None, "text": None, "report": {}, "errors": errors, "warnings": warnings or []}


# --- reading the pin table -------------------------------------------------------------------------


def normalise_name(name: str) -> str:
    """Datasheet active-low spellings -> KiCad's overbar: /RST, !RST, RST#, RST* -> ~{RST}."""
    n = name.strip()
    m = re.fullmatch(r"[/!](\w[\w+\-.]*)", n) or re.fullmatch(r"(\w[\w+\-.]*)[#*]", n)
    if m:
        return f"~{{{m.group(1)}}}"
    return n


def _read_pin(spec: dict, index: int, warnings: list[str]) -> Pin:
    if not isinstance(spec, dict):
        raise ValueError(f"pin {index + 1}: expected an object with number, name and type, got {spec!r}")
    if spec.get("number") in (None, ""):
        raise ValueError(f"pin {index + 1} ({spec.get('name', '?')}): needs a number")
    number = str(spec["number"]).strip()
    raw_name = str(spec.get("name") or "").strip()
    name = normalise_name(raw_name)
    if name != raw_name:
        warnings.append(f"pin {number}: name {raw_name!r} written as {name!r} (KiCad overbar)")
    if " " in name:
        name = name.replace(" ", "_")
        warnings.append(f"pin {number}: spaces in the name replaced with _ ({name})")
    etype = _pin_type(spec.get("type"), name, number)
    shape = str(spec.get("shape") or "line")
    if shape not in PIN_SHAPES:
        raise ValueError(f"pin {number}: shape must be one of {PIN_SHAPES}, got {shape!r}")
    unit = int(spec.get("unit") or 1)
    if unit < 1:
        raise ValueError(f"pin {number}: unit numbers start at 1")
    side = spec.get("side")
    if side is not None and side not in ("left", "right", "top", "bottom"):
        raise ValueError(f"pin {number}: side must be left, right, top or bottom, got {side!r}")
    alternates = []
    for alt in spec.get("alternates") or []:
        if isinstance(alt, str):
            alternates.append((normalise_name(alt), etype, "line"))
        else:
            alternates.append((normalise_name(str(alt["name"])), _pin_type(alt.get("type") or etype, alt["name"], number), alt.get("shape") or "line"))
    pin = Pin(number=number, name=name, etype=etype, shape=shape, unit=unit, hidden=bool(spec.get("hidden")), alternates=alternates)
    # Layout hints ride along on the pin until _layout has placed it.
    pin._side = side  # type: ignore[attr-defined]
    pin._group = str(spec["group"]) if spec.get("group") not in (None, "") else None  # type: ignore[attr-defined]
    pin._order = index  # type: ignore[attr-defined]
    return pin


def _pin_type(value, name: str, number: str) -> str:
    if value in (None, ""):
        if NO_CONNECT.match(name):
            return "no_connect"
        if GROUND.match(name) or POSITIVE.match(name):
            return "power_in"
        raise ValueError(f"pin {number} ({name}): needs a type ({', '.join(PIN_TYPES)})")
    v = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if v in PIN_TYPES:
        return v
    if v in TYPE_ALIASES:
        return TYPE_ALIASES[v]
    raise ValueError(f"pin {number} ({name}): unknown type {value!r}; use one of {PIN_TYPES}")


def _table_errors(table: list[Pin], name: str, reference: str) -> list[str]:
    errors = []
    seen: dict[tuple[str, int], Pin] = {}
    for p in table:
        key = (p.number, p.unit)
        if key in seen:
            errors.append(f"pin number {p.number} is given twice ({seen[key].name} and {p.name})")
        seen[key] = p
    by_number: dict[str, set[int]] = {}
    for p in table:
        by_number.setdefault(p.number, set()).add(p.unit)
    for n, us in by_number.items():
        if len(us) > 1:
            errors.append(f"pin {n} is in units {sorted(us)}; a pin belongs to one unit")
    units = sorted({p.unit for p in table})
    if units != list(range(1, len(units) + 1)):
        errors.append(f"units {units} skip a number; number them 1, 2, 3 ...")
    if not name or not symbol_check.valid_name(name):
        errors.append(f"symbol name {name!r} may only use letters, digits and _-.+, (KLC 1.7)")
    if not re.fullmatch(r"[A-Z]+", reference or ""):
        errors.append(f"reference prefix {reference!r} should be capital letters, e.g. U, J, Q")
    return errors


def _pin_length(table: list[Pin], stacks: bool) -> float:
    longest = max(len(p.number) for p in table)
    length = GRID if longest <= 2 else 3.81 if longest == 3 else 5.08
    names = [(p.unit, p.name.lower()) for p in table if p.etype in ("power_in", "power_out", "output") and p.name]
    if stacks and len(names) != len(set(names)):
        length = max(length, 3.81)  # room along the pin for a native stack's numbers and braces, as KiCad's RP2040 has
    return length


# --- layout --------------------------------------------------------------------------------------


def text_width(name: str) -> float:
    """Rough drawn width of a pin name: overbar markup takes no room."""
    plain = re.sub(r"[~_^]\{([^}]*)\}", r"\1", name)
    return len(plain) * CHAR_WIDTH


def _stacks(pins: list[Pin], stack_power: bool) -> list[_Slot]:
    """One slot per pin drawn. Power and output pins sharing a name stack, as KLC S4.3 allows."""
    slots: list[_Slot] = []
    by_name: dict[str, _Slot] = {}
    for p in sorted(pins, key=lambda q: q._order):
        stackable = p.etype in ("power_in", "power_out", "output", "passive") and p.name and p.etype != "no_connect"
        key = p.name.lower()
        if stack_power and stackable and key in by_name:
            by_name[key].pins.append(p)
            continue
        slot = _Slot([p], p._group, p._order)
        slots.append(slot)
        if stackable:
            by_name[key] = slot
    out = []
    for s in slots:
        if len(s.pins) == 1:
            out.append(s)
            continue
        types = {p.etype for p in s.pins}
        if "power_in" in types:
            lead = min((p for p in s.pins if p.etype == "power_in"), key=_num_key)
            rest = [p for p in s.pins if p is not lead]
            out.append(_Slot([lead] + sorted(rest, key=_num_key), s.group, s.order))
        elif types in ({"output"}, {"power_out"}):
            out.append(_Slot(sorted(s.pins, key=_num_key), s.group, s.order))
        else:  # passives alone may not stack (KLC S4.3); keep each pin
            out += [_Slot([p], p._group, p._order) for p in s.pins]
    return sorted(out, key=lambda s: s.order)


def _num_key(p: Pin):
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", p.number)
    return (m.group(1), int(m.group(2)), "") if m else (p.number, 0, p.number)


def _auto_side(pin: Pin, converter: bool) -> str | None:
    if pin.etype == "power_in":
        if GROUND.match(pin.name):
            return "bottom"
        return "left" if converter else "top"
    if pin.etype == "power_out":
        return "right"
    if pin.etype in LEFT_TYPES:
        return "left"
    if pin.etype in RIGHT_TYPES:
        return "right"
    return None  # bidirectional, passive and the rest go where there is room


def _sides(slots: list[_Slot], warnings: list[str]) -> dict[str, list[_Slot]]:
    # A power converter (regulator, charger) takes its supply on the left and gives power out on the right
    # (KLC S4.2). A chip with a small internal regulator, like most MCUs, keeps its supplies on top.
    signals = [s for s in slots if s.lead.etype not in ("power_in", "power_out")]
    converter = any(p.etype == "power_out" for s in slots for p in s.pins) and len(signals) <= symbol_check.CONVERTER_MAX_SIGNALS
    sides: dict[str, list[_Slot]] = {"left": [], "right": [], "top": [], "bottom": []}
    # Groups move together: the first pin with a side decides, else the pins' types vote.
    groups: dict[str, list[_Slot]] = {}
    units: list[list[_Slot]] = []
    for s in slots:
        if s.group is None:
            units.append([s])
        elif s.group in groups:
            groups[s.group].append(s)
        else:
            groups[s.group] = [s]
            units.append(groups[s.group])
    count = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    for members in units:
        explicit = [s.lead._side for s in members if s.lead._side]
        if explicit:
            side = explicit[0]
        else:
            votes = [_auto_side(s.lead, converter) for s in members]
            lr = [v for v in votes if v in ("left", "right")]
            if votes[0] and all(v == votes[0] for v in votes):
                side = votes[0]
            elif lr and lr.count("left") != lr.count("right"):
                side = max(("left", "right"), key=lr.count)
            else:
                side = "left" if count["left"] <= count["right"] else "right"
        for s in members:
            own = s.lead._side or side
            sides[own].append(s)
            count[own] += 1
    return sides


def _column(slots: list[_Slot]) -> list[_Slot | None]:
    """Slots down one side, with an empty one between groups."""
    out: list[_Slot | None] = []
    prev = object()
    for s in slots:
        if out and s.group != prev and (s.group is not None or prev is not None):
            out.append(None)
        out.append(s)
        prev = s.group
    return out


STACK_NUMBER_WIDTH = 1.27  # KiCad 10 draws a native stack's numbers side by side across the pin, in braces
STACK_BRACES = 2.0


def _spread(col: list) -> list:
    """Keep empty slots either side of a stacked pin, which KiCad draws with all its numbers across it."""
    out: list = []
    for s in col:
        span = 1 if s is None or len(s.pins) == 1 else math.ceil((len(s.pins) * STACK_NUMBER_WIDTH + STACK_BRACES) / GRID)
        pad = span - 1
        out += [None] * (pad // 2) + [s] + [None] * (pad - pad // 2)
    return out


def _ceil_grid(v: float, base: float = GRID) -> float:
    return round(math.ceil(v / base - 1e-9) * base, 4)


def _layout(number: int, pins: list[Pin], length: float, stack_power: bool, report: dict, warnings: list[str]) -> _Unit:
    nc = [p for p in pins if p.etype == "no_connect"]
    for p in nc:
        p.hidden = True  # KLC S4.6
    slots = _stacks([p for p in pins if p.etype != "no_connect"], stack_power)
    sides = _sides(slots, warnings)
    left, right = _spread(_column(sides["left"])), _spread(_column(sides["right"]))
    top, bottom = _spread(sides["top"]), _spread(sides["bottom"])

    def names(col):
        return [s.lead.name for s in col if s is not None]

    lw = max((text_width(n) for n in names(left)), default=0.0)
    rw = max((text_width(n) for n in names(right)), default=0.0)
    tw = max((text_width(n) for n in names(top)), default=0.0)
    bw = max((text_width(n) for n in names(bottom)), default=0.0)

    # Half width: room for the names on both sides, and for the top and bottom pins.
    across = max(len(top), len(bottom))
    half_w = max(MIN_HALF_WIDTH, (lw + rw) / 2 + PIN_NAME_OFFSET + GAP / 2, (math.ceil((across - 1) / 2) + 1) * GRID if across else 0)
    half_w = _ceil_grid(half_w + length) - length  # pin ends land on the grid

    # Rows on the 100 mil grid; each side centred on the origin, rounding up.
    def ys(col):
        first = math.ceil((len(col) - 1) / 2) * GRID
        return [round(first - k * GRID, 4) for k in range(len(col))]

    def xs(col):
        first = -math.floor((len(col) - 1) / 2) * GRID
        return [round(first + k * GRID, 4) for k in range(len(col))]

    ly, ry, tx, bx = ys(left), ys(right), xs(top), xs(bottom)
    rows = [(y, s, -half_w + PIN_NAME_OFFSET, -half_w + PIN_NAME_OFFSET + text_width(s.lead.name)) for y, s in zip(ly, left) if s]
    rows += [(y, s, half_w - PIN_NAME_OFFSET - text_width(s.lead.name), half_w - PIN_NAME_OFFSET) for y, s in zip(ry, right) if s]
    # Half height: one grid past the outer rows, and enough that the top and bottom
    # pin names, which run down (up) into the body, clear the side names under them.
    need = max([abs(y) + GRID for y, *_ in rows], default=GRID)
    for col, cx, sign in ((top, tx, 1), (bottom, bx, -1)):
        for s, x in zip(col, cx):
            if s is None:
                continue
            reach = PIN_NAME_OFFSET + text_width(s.lead.name)
            need = max(need, reach + FONT)
            for y, _, x1, x2 in rows:
                if x1 - 0.8 <= x <= x2 + 0.8:
                    need = max(need, sign * y + reach + FONT + NAME_CLEARANCE)
    if not left and not right and top and bottom:
        need = max(need, (tw + bw) / 2 + PIN_NAME_OFFSET + FONT)
    half_h = _ceil_grid(need + length) - length

    placed: list[Pin] = []

    def place(slot: _Slot, x: float, y: float, rotation: int):
        # A stack is one pin in KiCad 10, numbered with all its pads (KLC S4.3).
        p = slot.lead
        if len(slot.pins) > 1:
            p.number = symbol.stack_number([q.number for q in slot.pins])
        p.x, p.y, p.rotation, p.length = x, y, rotation, length
        placed.append(p)

    edge_x = round(half_w + length, 4)
    edge_y = round(half_h + length, 4)
    for s, y in zip(left, ly):
        if s:
            place(s, -edge_x, y, 0)
    for s, y in zip(right, ry):
        if s:
            place(s, edge_x, y, 180)
    for col, cx, y, rot in ((top, tx, edge_y, 270), (bottom, bx, -edge_y, 90)):
        for s, x in zip(col, cx):
            if s:
                place(s, x, y, rot)
    # Hidden no-connect pins inside the body, along the bottom edge, on the 50 mil grid (KLC S4.1 allows it for NC).
    per_row = max(1, int((2 * half_w - FONT) // GRID))
    for k, p in enumerate(sorted(nc, key=_num_key)):
        row, col = divmod(k, per_row)
        p.x = round(-half_w + FONT + col * GRID, 4)
        p.y = round(-half_h + FONT + row * GRID, 4)
        p.rotation, p.length = 90, 0.0
        placed.append(p)

    rect = Rect(-round(half_w, 4), -round(half_h, 4), round(half_w, 4), round(half_h, 4), OUTLINE_WIDTH, "background", number, 1)
    report["units"][number] = {
        "left": names(left), "right": names(right), "top": names(top), "bottom": names(bottom),
        "stacked": {s.lead.name: s.lead.number for s in slots if len(s.pins) > 1},
        "hidden_nc": [p.number for p in nc],
        "body": [rect.xmax - rect.xmin, rect.ymax - rect.ymin],
    }
    for p in placed:
        p.unit = number
    return _Unit(number, sorted(placed, key=_num_key), rect)


OPAMP = [(-5.08, 5.08), (5.08, 0.0), (-5.08, -5.08), (-5.08, 5.08)]  # KiCad's opamp triangle
OPAMP_NAME_OFFSET = 0.127


def _is_negative_supply(p: Pin) -> bool:
    n = p.name.replace(" ", "").upper()
    return bool(GROUND.match(p.name)) or "-" in n or n.startswith(("VEE", "VSS"))


def _supplies_only(pins: list[Pin]) -> bool:
    neg = [p for p in pins if p.etype == "power_in" and _is_negative_supply(p)]
    return all(p.etype == "power_in" for p in pins) and len(neg) <= 1 and len(pins) - len(neg) <= 1


def _layout_supply_unit(number: int, pins: list[Pin], report: dict) -> _Unit:
    """The supply unit of a multi-amplifier part, drawn as KiCad's: no body, the pins where an amplifier's supplies go."""
    for p in pins:
        neg = _is_negative_supply(p)
        # KiCad's own sit 100 mil left, under an amplifier's supply pins; centred keeps KLC S3.1.
        p.x, p.y, p.rotation, p.length, p.unit = 0.0, -7.62 if neg else 7.62, 90 if neg else 270, 3.81, number
    report["units"][number] = {"shape": "supplies", "supplies": [p.number for p in pins]}
    return _Unit(number, sorted(pins, key=_num_key), box_hint=Rect(-5.08, -3.81, 5.08, 3.81))


def _layout_opamp(number: int, pins: list[Pin], report: dict, warnings: list[str]) -> _Unit:
    """An amplifier unit drawn as KiCad's opamps are: + and - in on the left, out on the right, supplies above and below."""
    plus, minus, out, sup_pos, sup_neg = [], [], [], [], []
    for p in pins:
        n = p.name.replace(" ", "").upper()
        if p.etype == "power_in":
            (sup_neg if _is_negative_supply(p) else sup_pos).append(p)
        elif p.etype in ("output", "tri_state", "open_collector", "open_emitter"):
            out.append(p)
        elif n.endswith("+") or n.startswith("+") or n in ("INP", "IN_P", "NONINV"):
            plus.append(p)
        elif n.endswith("-") or n.startswith("-") or n in ("INN", "IN_N", "INV"):
            minus.append(p)
        else:
            raise ValueError(f"unit {number} is drawn as an opamp, which only takes +, - and output pins and supplies; "
                             f"pin {p.number} ({p.name}) fits none. Draw this unit as a rectangle")
    if len(plus) != 1 or len(minus) != 1 or len(out) != 1 or len(sup_pos) > 1 or len(sup_neg) > 1:
        raise ValueError(f"unit {number}: an opamp unit needs one +, one - and one output pin, and at most one supply each way; "
                         f"got + {[p.number for p in plus]}, - {[p.number for p in minus]}, out {[p.number for p in out]}")
    renamed = []
    for p, new in ((plus[0], "+"), (minus[0], "-"), (out[0], "")):
        if p.name != new:
            renamed.append(f"{p.number} {p.name or '(none)'} -> {new}")
            p.name = new
    if renamed:
        warnings.append(f"unit {number}: pin names shown as KiCad's opamps show them: {', '.join(renamed)}")
    # Positions from KiCad's own (LM2904): inputs 100 mil above and below the axis, supplies 100 mil left of centre.
    for p, x, y, rot, length in ((plus[0], -7.62, 2.54, 0, 2.54), (minus[0], -7.62, -2.54, 0, 2.54), (out[0], 7.62, 0.0, 180, 2.54)):
        p.x, p.y, p.rotation, p.length = x, y, rot, length
    for group, y, rot in ((sup_pos, 7.62, 270), (sup_neg, -7.62, 90)):
        for p in group:
            p.x, p.y, p.rotation, p.length = -2.54, y, rot, 3.81
    report["units"][number] = {"shape": "opamp", "plus": plus[0].number, "minus": minus[0].number, "output": out[0].number,
                               "supplies": [p.number for p in sup_pos + sup_neg]}
    for p in pins:
        p.unit = number
    return _Unit(number, sorted(pins, key=_num_key), outline=Polyline(list(OPAMP), OUTLINE_WIDTH, "background", number, 1))


def _interchangeable(units: list[_Unit]) -> bool:
    def shape(u: _Unit):
        return sorted((p.name, p.etype, p.side, round(p.y, 3)) for p in u.pins)

    return all(shape(u) == shape(units[0]) for u in units[1:])


# --- fields --------------------------------------------------------------------------------------


def footprint_filters(footprint: str) -> list[str]:
    """KiCad-style footprint filters from a footprint name.

    Package_SO:SOIC-16_3.9x9.9mm_P1.27mm -> SOIC*3.9x9.9mm*P1.27mm*; the pin count is left
    out because KiCad already matches it, and dashes become ? as in KiCad's own filters.
    """
    name = footprint.split(":", 1)[-1]
    tokens = name.split("_")
    head = re.sub(r"-\d+(-\d*EP)?$", lambda m: ("*" + m.group(1)[1:]) if m.group(1) else "", tokens[0])
    head = head.replace("-", "?")
    return ["*".join([head] + tokens[1:]) + "*"]


def _filters(fp_filters, footprint: str | None) -> list[str]:
    if fp_filters:
        return fp_filters.split() if isinstance(fp_filters, str) else [str(f) for f in fp_filters]
    return footprint_filters(footprint) if footprint else []


def _fields(sym: Symbol, reference, footprint, datasheet, description, keywords, filters, units: list[_Unit], length: float):
    # The fields are shared by all units, so they go above and below every unit's pins,
    # aligned to the corners of the narrowest body.
    shown = [p for p in sym.pins if not p.hidden]
    top = max([u.box.ymax for u in units] + [p.y for p in shown])
    bottom = min([u.box.ymin for u in units] + [p.y for p in shown])
    if any(p.side == "top" and len(p.numbers) > 1 for p in shown):
        top += 1.27  # a stack's braces reach past the pin end
    narrow = min((u.box for u in units), key=lambda r: r.xmax - r.xmin)
    rect = symbol.Rect(narrow.xmin, bottom, narrow.xmax, top)
    ref_y = _ceil_grid(top + 1.27, 1.27)
    width = rect.xmax - rect.xmin
    props = {"Reference": symbol.Property(reference, rect.xmin, ref_y, False, "left")}
    if (len(reference) + 2 + len(sym.name)) * CHAR_WIDTH + 2.54 <= width:
        props["Value"] = symbol.Property(sym.name, rect.xmax, ref_y, False, "right")
    else:
        props["Value"] = symbol.Property(sym.name, rect.xmin, -_ceil_grid(-bottom + 1.27, 1.27), False, "left")
    hidden_y = min(props["Value"].y, -_ceil_grid(-bottom + 1.27, 1.27)) - 2.54
    props["Footprint"] = symbol.Property(footprint or "", rect.xmin, hidden_y, True, "left")
    props["Datasheet"] = symbol.Property(datasheet or "", rect.xmin, hidden_y - 2.54, True, "left")
    props["Description"] = symbol.Property(description or "", rect.xmin, hidden_y - 5.08, True, "left")
    if keywords:
        props["ki_keywords"] = symbol.Property(" ".join(str(keywords).split()), 0, 0, True)
    if filters:
        props["ki_fp_filters"] = symbol.Property(" ".join(filters), 0, 0, True)
    return props


# --- writer --------------------------------------------------------------------------------------


def _f(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _effects(indent: str, justify: str = "") -> str:
    out = f"{indent}(effects\n{indent}\t(font\n{indent}\t\t(size 1.27 1.27)\n{indent}\t)\n"
    if justify:
        out += f"{indent}\t(justify {justify})\n"
    return out + f"{indent})\n"


def _property(key: str, value: str, x: float, y: float, hidden: bool, justify: str = "", private: bool = False) -> str:
    q = sexpr.quote
    out = f"\t\t(property {'private ' if private else ''}{q(key)} {q(value)}\n\t\t\t(at {_f(x)} {_f(y)} 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n"
    if hidden:
        out += "\t\t\t(hide yes)\n"
    return out + _effects("\t\t\t", justify) + "\t\t)\n"


def to_kicad_sym(sym: Symbol) -> str:
    """The symbol block in KiCad 10 syntax, as KiCad's symbol editor writes it, indented to sit inside a kicad_symbol_lib."""
    q = sexpr.quote
    out = [f"\t(symbol {q(sym.name)}\n"]
    add = out.append
    if sym.pin_names_offset != 0.508:
        add(f"\t\t(pin_names\n\t\t\t(offset {_f(sym.pin_names_offset)})\n\t\t)\n")
    add("\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n\t\t(duplicate_pin_numbers_are_jumpers no)\n")
    for key, p in sym.properties.items():
        if key == "ki_fp_filters" and sym.units_locked:
            add(_property("ki_locked", "", 0, 0, False))  # where KiCad puts it
        add(_property(key, p.value, p.x, p.y, p.hidden, p.justify, p.private))
    if sym.units_locked and "ki_fp_filters" not in sym.properties:
        add(_property("ki_locked", "", 0, 0, False))
    for unit in sorted({r.unit for r in sym.rects} | {g.unit for g in sym.polylines} | {p.unit for p in sym.pins}):
        add(f"\t\t(symbol {q(f'{sym.name}_{unit}_1')}\n")
        for r in (r for r in sym.rects if r.unit == unit):
            add(f"\t\t\t(rectangle\n\t\t\t\t(start {_f(r.xmin)} {_f(r.ymax)})\n\t\t\t\t(end {_f(r.xmax)} {_f(r.ymin)})\n"
                f"\t\t\t\t(stroke\n\t\t\t\t\t(width {_f(r.width)})\n\t\t\t\t\t(type default)\n\t\t\t\t)\n"
                f"\t\t\t\t(fill\n\t\t\t\t\t(type {r.fill})\n\t\t\t\t)\n\t\t\t)\n")
        for g in (g for g in sym.polylines if g.unit == unit):
            pts = " ".join(f"(xy {_f(x)} {_f(y)})" for x, y in g.pts)
            add(f"\t\t\t(polyline\n\t\t\t\t(pts {pts})\n\t\t\t\t(stroke\n\t\t\t\t\t(width {_f(g.width)})\n\t\t\t\t\t(type default)\n\t\t\t\t)\n"
                f"\t\t\t\t(fill\n\t\t\t\t\t(type {g.fill})\n\t\t\t\t)\n\t\t\t)\n")
        for p in (p for p in sym.pins if p.unit == unit):
            add(f"\t\t\t(pin {p.etype} {p.shape}\n\t\t\t\t(at {_f(p.x)} {_f(p.y)} {p.rotation})\n\t\t\t\t(length {_f(p.length)})\n")
            if p.hidden:
                add("\t\t\t\t(hide yes)\n")
            add(f"\t\t\t\t(name {q(p.name)}\n{_effects(chr(9) * 5)}\t\t\t\t)\n")
            add(f"\t\t\t\t(number {q(p.number)}\n{_effects(chr(9) * 5)}\t\t\t\t)\n")
            for name, etype, shape in p.alternates:
                add(f"\t\t\t\t(alternate {q(name)} {etype} {shape})\n")
            add("\t\t\t)\n")
        add("\t\t)\n")
    add("\t\t(embedded_fonts no)\n\t)\n")
    return "".join(out)
