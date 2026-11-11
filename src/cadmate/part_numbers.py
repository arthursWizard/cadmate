"""Part numbers onto the parts of an existing schematic: manufacturer, MPN, distributor numbers, package.

Most schematics are drawn with generic symbols (an R or a C with a value), so the numbers an assembler needs have to
be added part by part before the board can be ordered. This takes them for many parts at once, {ref: {field: value}},
and writes them into the schematic and the board beside it.

It edits sheets someone drew, so it changes only the fields named: a field's value is replaced where it stands, a
missing field is added after the symbol's other fields as a hidden copy of one of them, and every other byte of the
file stays as it was. A field already spelled another way (MFR Part Number, LCSC Part #) is updated under its own name,
the one export_fab would read, instead of getting a second field beside it.

Units of one part (U1A, U1B) all get the fields, as KiCad keeps them. A sheet used more than once is one set of
symbols in one file, so its fields are shared by every use of it: setting R1 there sets the copy's R101 too, and the
reply says so; different numbers for refs that share a symbol are refused.
"""

from __future__ import annotations

import re
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from . import fab_export, project, sexpr

CANON = {"manufacturer": "Manufacturer", "mpn": "MPN", "lcsc": "LCSC", "digikey": "DigiKey", "mouser": "Mouser",
         "package": "Package"}  # names for a new field when the schematic has no spelling of its own yet
DESIGN_FIELDS = ("reference", "value", "footprint")  # not part numbers: changing these changes the design
PROP_HEAD = re.compile(r'\(property\s+"(?:[^"\\]|\\.)*"\s+"(?:[^"\\]|\\.)*"')


class PartNumberError(ValueError):
    pass


@dataclass
class Sym:
    """One placed symbol (one unit of one part) in one sheet file."""
    file: Path
    node: sexpr.Node
    refs: list[str]
    value: str = ""
    footprint: str = ""


@dataclass
class Sheet:
    path: Path
    text: str
    root: sexpr.Node
    edits: list[tuple[int, int, str]] = field(default_factory=list)


def _props(node: sexpr.Node) -> list[sexpr.Node]:
    return [p for p in node.children("property") if p.atoms()]


def _prop_value(node: sexpr.Node, name: str) -> str:
    for p in _props(node):
        a = p.atoms()
        if a[0] == name:
            return a[1] if len(a) > 1 else ""
    return ""


def _sheet_files(root_sch: Path) -> list[Sheet]:
    """The root sheet and every sheet below it, each file once (a sheet used twice is one file)."""
    out: list[Sheet] = []
    seen: set[Path] = set()
    todo = [root_sch]
    while todo:
        f = todo.pop(0)
        key = f.resolve()
        if key in seen:
            continue
        seen.add(key)
        if not f.exists():
            raise PartNumberError(f"{f} is in the hierarchy but isn't there")
        text = f.read_bytes().decode("utf-8")  # not read_text: line endings stay as they are
        try:
            root = sexpr.parse(text)
        except sexpr.SexprError as e:
            raise PartNumberError(f"{f.name} isn't a KiCad schematic: {e}") from None
        if root.head != "kicad_sch":
            raise PartNumberError(f"{f.name} isn't a KiCad schematic")
        out.append(Sheet(f, text, root))
        for s in root.children("sheet"):
            name = _prop_value(s, "Sheetfile") or _prop_value(s, "Sheet file")  # KiCad 7+, KiCad 6
            if name:
                todo.append(f.parent / name)
    return out


def _symbols(sheets: list[Sheet], project_name: str) -> list[Sym]:
    # KiCad 6 keeps the references of every sheet's symbols in the root sheet, keyed by path ending in the symbol uuid
    legacy: dict[str, list[str]] = {}
    si = sheets[0].root.child("symbol_instances")
    if si is not None:
        for p in si.children("path"):
            r = p.child("reference")
            if p.atoms() and r is not None and r.atoms():
                legacy.setdefault(p.atoms()[0].rsplit("/", 1)[-1], []).append(r.atoms()[0])
    out = []
    for sh in sheets:
        for node in sh.root.children("symbol"):
            refs: list[tuple[str, str]] = []
            inst = node.child("instances")
            if inst is not None:
                for proj in inst.children("project"):
                    pname = (proj.atoms() or [""])[0]
                    for p in proj.children("path"):
                        r = p.child("reference")
                        if r is not None and r.atoms():
                            refs.append((pname, r.atoms()[0]))
            # a sheet shared between projects lists the other projects' references too
            mine = [r for n, r in refs if n == project_name] or [r for _, r in refs]
            if not mine:
                u = node.child("uuid")
                mine = legacy.get((u.atoms() or [""])[0] if u is not None else "", []) or [_prop_value(node, "Reference")]
            mine = list(dict.fromkeys(r for r in mine if r and not r.startswith("#") and "?" not in r))
            if mine:
                out.append(Sym(sh.path, node, mine, _prop_value(node, "Value"), _prop_value(node, "Footprint")))
    return out


def ref_of(node: sexpr.Node, sch: bool) -> list[str]:
    """The references a placed symbol (every instance of it) or a footprint goes by."""
    refs = [_prop_value(node, "Reference")]
    inst = node.child("instances") if sch else None
    if inst is not None:
        refs += [r.atoms()[0] for proj in inst.children("project") for p in proj.children("path")
                 for r in [p.child("reference")] if r is not None and r.atoms()]
    return list(dict.fromkeys(refs))


def _target(node: sexpr.Node, name: str) -> sexpr.Node | None:
    """The field a value for name goes into: for a part-number field, the one export_fab reads (the first filled one
    of its spellings, else the first present); otherwise the field of that name."""
    props = _props(node)
    kind = fab_export.field_kind(name)
    if kind is None:
        return next((p for p in props if p.atoms()[0] == name), None)
    by_norm: dict[str, sexpr.Node] = {}
    for p in props:
        by_norm.setdefault(fab_export._norm(p.atoms()[0]), p)
    spellings = [by_norm[n] for n in fab_export.FIELDS[kind] if n in by_norm]
    return next((p for p in spellings if (p.atoms()[1:2] or [""])[0].strip()), spellings[0] if spellings else None)


def _hidden(p: sexpr.Node) -> bool:
    for n in (p, p.child("effects")):
        if n is None:
            continue
        h = n.child("hide")
        if (h is not None and (h.atoms() or ["yes"])[0] == "yes") or (n is not p and "hide" in n.atoms()):
            return True
    return False


def _indent(text: str, pos: int) -> str:
    line = text.rfind("\n", 0, pos) + 1
    lead = text[line:pos]
    return "\n" + lead if not lead.strip() else " "


def _set(text: str, node: sexpr.Node, name: str, value: str, board: bool, version: int,
         new_name: str | None = None) -> tuple[str, str, list]:
    """(field name written, old value, edits) to make node's field read value; no edits when it already does.
    A field that isn't there yet is added as new_name (default: name)."""
    q = sexpr.quote
    p = _target(node, name)
    if p is not None:
        old = (p.atoms()[1:2] or [""])[0]
        if old == value:
            return p.atoms()[0], old, []
        m = PROP_HEAD.match(text, p.start)
        if m is None:  # (property "Name") with no value: KiCad never writes one, but don't guess at it
            raise PartNumberError(f"can't read the {p.atoms()[0]} field at offset {p.start}")
        return p.atoms()[0], old, [(p.start, m.end(), f"(property {q(p.atoms()[0])} {q(value)}")]
    name = new_name or name
    if not value:
        return name, "", []
    props = _props(node)
    if not props:
        raise PartNumberError(f"a symbol or footprint at offset {node.start} has no fields to copy the format from")
    last = props[-1]
    tmpl = next((t for t in props if t.atoms()[0] not in ("Reference", "Value") and _hidden(t)), None)
    if tmpl is not None and len(tmpl.atoms()) >= 2:
        new = text[tmpl.start:tmpl.end]
        new = f"(property {q(name)} {q(value)}" + new[PROP_HEAD.match(new).end():]
        if tmpl.child("id") is not None:  # KiCad 6 and 7 number their fields
            top = max((int(i.atoms()[0]) for i in (x.child("id") for x in props) if i is not None and i.atoms()
                       and i.atoms()[0].isdigit()), default=0)
            new = re.sub(r"\(id\s+\d+\)", f"(id {top + 1})", new, count=1)
        new = re.sub(r'\(uuid\s+"?[0-9a-fA-F-]+"?\s*\)', lambda _m: f'(uuid "{uuidlib.uuid4()}")', new)
    elif board and len(last.atoms()) == 2 and len(last.items) == 3:  # KiCad 7 boards: (property "Name" "value")
        new = f"(property {q(name)} {q(value)})"
    elif board:
        new = (f'(property {q(name)} {q(value)} (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{uuidlib.uuid4()}") '
               f"(effects (font (size 1 1) (thickness 0.15))))")
    else:
        at = node.child("at")
        xy = " ".join((at.atoms() if at is not None else ["0", "0"])[:2])
        hide = ("(hide yes) (effects (font (size 1.27 1.27)))" if version >= 20240101
                else "(effects (font (size 1.27 1.27)) (hide yes))" if version >= 20231120
                else "(effects (font (size 1.27 1.27)) hide)")
        new = f"(property {q(name)} {q(value)} (at {xy} 0) {hide})"
    return name, "", [(last.end, last.end, _indent(text, last.start) + new)]


def _version(root: sexpr.Node) -> int:
    v = root.child("version")
    try:
        return int((v.atoms() or ["0"])[0]) if v is not None else 0
    except ValueError:
        return 0


def _lock(f: Path) -> Path | None:
    """KiCad's lock file for f while an editor has it open (~name.lck; a plain name.lck in older versions)."""
    for lk in (f.parent / f"~{f.name}.lck", f.parent / f"{f.name}.lck"):
        if lk.exists():
            return lk
    return None


def _apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    # from the end back, so offsets hold; insertions at one spot come out in the order given
    for _, (a, b, r) in sorted(enumerate(edits), key=lambda e: (e[1][0], e[1][1], e[0]), reverse=True):
        text = text[:a] + r + text[b:]
    return text


def _expand(parts: dict) -> dict[str, dict[str, str]]:
    """{ref or "R1,R2,R5": {field: value}} -> {ref: {field: value}}."""
    if not isinstance(parts, dict) or not parts:
        raise PartNumberError('parts must be {ref: {field: value}}, e.g. {"R1,R2": {"MPN": "...", "LCSC": "C..."}}')
    out: dict[str, dict[str, str]] = {}
    for key, fields in parts.items():
        if not isinstance(fields, dict) or not fields:
            raise PartNumberError(f"{key}: give the fields as {{name: value}}")
        clean = {}
        for name, value in fields.items():
            name = str(name).strip()
            if not name:
                raise PartNumberError(f"{key}: a field with no name")
            if fab_export._norm(name) in DESIGN_FIELDS:
                raise PartNumberError(f"{key}: {name} is part of the design, not a part number; change it in KiCad")
            clean[name] = "" if value is None else str(value).strip()
        for ref in (r.strip() for r in str(key).split(",")):
            if not ref:
                continue
            if ref in out and any(out[ref].get(n, v) != v for n, v in clean.items()):
                raise PartNumberError(f"{ref} is given twice with different values")
            out.setdefault(ref, {}).update(clean)
    return out


def set_part_numbers(schematic: str, parts: dict, board: str | None = None, dry_run: bool = False) -> dict:
    sch = Path(schematic).expanduser()
    if sch.suffix != ".kicad_sch" or not sch.exists():
        raise PartNumberError(f"no .kicad_sch at {sch}")
    want = _expand(parts)
    sheets = _sheet_files(sch)
    syms = _symbols(sheets, sch.stem)
    by_ref: dict[str, list[Sym]] = {}
    for s in syms:
        for r in s.refs:
            by_ref.setdefault(r, []).append(s)
    unknown = sorted((r for r in want if r not in by_ref), key=fab_export._refkey)
    sheet_of = {sh.path: sh for sh in sheets}

    # what each symbol gets: refs sharing one symbol (a sheet used twice) must agree
    conflicts, shared = [], []
    per_sym: dict[int, dict[str, str]] = {}
    skip: set[str] = set()
    for s in syms:
        given = [r for r in s.refs if r in want]
        if not given:
            continue
        fields: dict[str, str] = {}
        bad = False
        for r in given:
            for n, v in want[r].items():
                if fields.setdefault(n, v) != v:
                    bad = True
        if bad:
            conflicts.append({"refs": given, "why": "these share one symbol (the same sheet used more than once), so "
                                                    "they can't have different numbers; nothing was set for them"})
            skip.update(s.refs)
            continue
        per_sym[id(s)] = fields
        if set(s.refs) - set(given):
            entry = {"refs": sorted(s.refs, key=fab_export._refkey), "given": sorted(given, key=fab_export._refkey)}
            if entry not in shared:
                shared.append(entry)
    for s in syms:
        if id(s) in per_sym and skip & set(s.refs):
            del per_sym[id(s)]

    # a new field takes the spelling this schematic already uses for that number most often, else cadmate's
    seen: dict[str, dict[str, int]] = {}
    for s in syms:
        for p in _props(s.node):
            kind = fab_export.field_kind(p.atoms()[0])
            if kind:
                seen.setdefault(kind, {}).setdefault(p.atoms()[0], 0)
                seen[kind][p.atoms()[0]] += 1

    def spelling(name: str) -> str:
        kind = fab_export.field_kind(name)
        if kind is None:
            return name
        return max(seen[kind].items(), key=lambda e: e[1])[0] if seen.get(kind) else CANON[kind]

    changed: dict[str, dict] = {}
    written_as: dict[str, dict[str, str]] = {}  # ref -> {field asked for: field written in the schematic}
    already: set[str] = set()
    for s in syms:
        fields = per_sym.get(id(s))
        if not fields:
            continue
        sh = sheet_of[s.file]
        for n, v in fields.items():
            written, old, edits = _set(sh.text, s.node, n, v, board=False, version=_version(sh.root),
                                       new_name=spelling(n))
            sh.edits += edits
            for r in s.refs:
                written_as.setdefault(r, {})[n] = written
                if edits:
                    entry = changed.setdefault(r, {"value": s.value, "footprint": s.footprint, "fields": {}})
                    entry["fields"][written] = {"from": old, "to": v}
                else:
                    already.add(r)
    # a field set by two specs at one spot (a copy of the same edit for each ref) is applied once
    for sh in sheets:
        sh.edits = list(dict.fromkeys(sh.edits))
        starts = [e[0] for e in sh.edits if e[0] != e[1]]
        if len(starts) != len(set(starts)):
            raise PartNumberError(f"two different edits to one field in {sh.path.name} (a cadmate bug: please report it)")
    already -= set(changed)

    # the board beside the schematic: footprints by reference, the values each ref's symbol now has
    pcb = Path(board).expanduser() if board else sch.with_suffix(".kicad_pcb")
    board_out: dict = {}
    pcb_text = pcb_new = None
    if pcb.exists():
        pcb_text = pcb.read_bytes().decode("utf-8")
        try:
            proot = sexpr.parse(pcb_text)
        except sexpr.SexprError as e:
            raise PartNumberError(f"{pcb.name} isn't a KiCad board: {e}") from None
        fps: dict[str, list[sexpr.Node]] = {}
        for fp in proot.children("footprint"):
            fps.setdefault(_prop_value(fp, "Reference"), []).append(fp)
        ref_fields = {r: per_sym[id(s)] for s in syms if id(s) in per_sym for r in s.refs}  # units share them
        edits, bchanged, missing = [], {}, []
        for r, fields in ref_fields.items():
            if r not in fps:
                missing.append(r)
                continue
            for fp in fps[r]:
                for n, v in fields.items():
                    # the board's own spelling if it has one, else the schematic's, as Update PCB would add it
                    written, old, e = _set(pcb_text, fp, n, v, board=True, version=_version(proot),
                                           new_name=written_as.get(r, {}).get(n, n))
                    if e:
                        edits += e
                        bchanged.setdefault(r, {})[written] = {"from": old, "to": v}
        if edits:
            pcb_new = _apply(pcb_text, edits)
        board_out = {"path": str(pcb), "changed": bchanged}
        if missing:
            board_out["not_on_board"] = sorted(missing, key=fab_export._refkey)
            board_out["not_on_board_note"] = "parts in the schematic with no footprint on the board: Update PCB from " \
                                             "Schematic in KiCad, then run this again to carry the numbers over"
    else:
        board_out = {"path": None, "note": f"no board at {pcb}; KiCad's Update PCB from Schematic carries the numbers "
                                           "over when there is one"}

    writes = [(sh.path, _apply(sh.text, sh.edits)) for sh in sheets if sh.edits]
    if pcb_new is not None:
        writes.append((pcb, pcb_new))
    for f, text in writes:  # the edits must leave a file that parses, with the same items, the fields reading as asked
        old = sheet_of[f].root if f in sheet_of else sexpr.parse(pcb_text)
        new = sexpr.parse(text)
        ok = len(new.items) == len(old.items)
        heads = ("symbol",) if f in sheet_of else ("footprint",)
        for node in (n for h in heads for n in new.children(h)):
            refs = [r for r in ref_of(node, f in sheet_of) if r in want and r not in skip]
            for r in refs:
                for n, v in want[r].items():
                    t = _target(node, n)
                    ok &= (t.atoms()[1:2] or [""])[0] == v if t is not None else not v
        if not ok:
            raise PartNumberError(f"editing {f.name} didn't come out as planned, so nothing was written (a cadmate bug: "
                                  "please report it)")

    warnings = []
    for r, fields in want.items():
        for n, v in fields.items():
            if fab_export.field_kind(n) == "lcsc" and v and fab_export.placeholder(v):
                warnings.append(f"{r}: LCSC number {v!r} isn't C followed by digits")
    for key in parts:
        refs = [r.strip() for r in str(key).split(",") if r.strip() in by_ref]
        values = {by_ref[r][0].value for r in refs}
        if len(values) > 1:
            warnings.append(f"{key}: one set of numbers for parts with different values ({', '.join(sorted(values))}); "
                            "check they really are the same part")

    result: dict = {"ok": True, "dry_run": dry_run, "schematic": str(sch),
                     "changed": {r: changed[r] for r in sorted(changed, key=fab_export._refkey)},
                     "already_set": sorted(already, key=fab_export._refkey), "board": board_out}
    if unknown:
        result["unknown_refs"] = unknown
    if shared:
        result["shared_symbols"] = shared
        result["shared_note"] = "these refs are one symbol on a sheet used more than once: setting one sets them all"
    if conflicts:
        result["conflicts"] = conflicts
    if warnings:
        result["warnings"] = warnings
    if any(sexpr.parse(t).child("generator") is not None and sexpr.parse(t).child("generator").atoms()[:1] == ["cadmate"]
           for f, t in writes if f.suffix == ".kicad_sch"):
        result["cadmate_sheet_note"] = "write_schematic draws this sheet from its inputs: put these numbers in its parts' " \
                                       "fields too, or writing it again with replace=True drops them"
    if dry_run or not writes:
        result["files_written"] = []
        result["next"] = ("nothing to change" if not writes else "dry run: nothing written; call again without dry_run")
        return result

    locked = [str(f.name) for f, _ in writes if _lock(f)]
    if locked:
        raise PartNumberError(f"KiCad has {', '.join(locked)} open, so its next save would undo this: close the "
                              "schematic and board in KiCad first (if KiCad isn't running, delete the stale ~*.lck "
                              "file beside it)")
    kept = project.keep([f for f, _ in writes], "part-numbers")
    for f, text in writes:
        f.write_bytes(text.encode("utf-8"))
    result["files_written"] = [str(f) for f, _ in writes]
    if kept:
        result["previous_kept"] = kept
    result["next"] = "check_ready_to_order to see what still has no number; export_fab puts the numbers in the BOM"
    return result
