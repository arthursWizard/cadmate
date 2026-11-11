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

import os
import re
import tempfile
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
    paths: set[str] = field(default_factory=set)  # where the sheet sits in the hierarchy: /root/sheet/... uuids
    edits: list[tuple[int, int, str]] = field(default_factory=list)


def _props(node: sexpr.Node) -> list[sexpr.Node]:
    return [p for p in node.children("property") if p.atoms()]


def _prop_value(node: sexpr.Node, name: str) -> str:
    for p in _props(node):
        a = p.atoms()
        if a[0] == name:
            return a[1] if len(a) > 1 else ""
    return ""


def _uuid_of(node: sexpr.Node) -> str:
    u = node.child("uuid")
    return (u.atoms() or [""])[0] if u is not None else ""


def _sheet_files(root_sch: Path) -> list[Sheet]:
    """The root sheet and every sheet below it, each file once (a sheet used twice is one file), with the hierarchy
    paths it's used at, as KiCad names a sheet's instances: /<root uuid>/<sheet uuid>/..."""
    out: dict[Path, Sheet] = {}
    todo = [(root_sch, None)]
    while todo:
        f, path = todo.pop(0)
        key = f.resolve()
        if key not in out:
            if not f.exists():
                raise PartNumberError(f"{f} is in the hierarchy but isn't there")
            text = f.read_bytes().decode("utf-8")  # not read_text: line endings stay as they are
            try:
                root = sexpr.parse(text)
            except sexpr.SexprError as e:
                raise PartNumberError(f"{f.name} isn't a KiCad schematic: {e}") from None
            if root.head != "kicad_sch":
                raise PartNumberError(f"{f.name} isn't a KiCad schematic")
            out[key] = Sheet(f, text, root)
        sh = out[key]
        if path is None:
            path = "/" + _uuid_of(sh.root)
        if path in sh.paths or path.count("/") > 32:  # a sheet that includes itself
            continue
        sh.paths.add(path)
        for s in sh.root.children("sheet"):
            name = _prop_value(s, "Sheetfile") or _prop_value(s, "Sheet file")  # KiCad 7+, KiCad 6
            if name:
                todo.append((f.parent / name, f"{path}/{_uuid_of(s)}"))
    return list(out.values())


def _symbols(sheets: list[Sheet]) -> list[Sym]:
    """Every placed symbol with the references it has where its sheet is used. Only instances at a path the hierarchy
    has count: a symbol keeps the paths of sheets since deleted or of other projects, with references that may now
    belong to other parts."""
    # KiCad 6 keeps the references of every sheet's symbols in the root sheet, by path to the symbol
    legacy: dict[str, str] = {}
    root_path = "/" + _uuid_of(sheets[0].root)
    si = sheets[0].root.child("symbol_instances")
    if si is not None:
        for p in si.children("path"):
            r = p.child("reference")
            if p.atoms() and r is not None and r.atoms():
                legacy[p.atoms()[0]] = r.atoms()[0]
    out = []
    for sh in sheets:
        for node in sh.root.children("symbol"):
            inst = node.child("instances")
            listed = [] if inst is None else [(p.atoms()[0], r.atoms()[0]) for proj in inst.children("project")
                                              for p in proj.children("path") for r in [p.child("reference")]
                                              if p.atoms() and r is not None and r.atoms()]
            mine = [r for path, r in listed if path in sh.paths]
            if not mine:
                # KiCad 6 leaves the root sheet's uuid out of these paths: /<sheet uuid>/<symbol uuid>
                keys = [k for p in sorted(sh.paths) for k in (f"{p}/{_uuid_of(node)}",
                                                               f"{p[len(root_path):]}/{_uuid_of(node)}")]
                mine = [legacy[k] for k in dict.fromkeys(keys) if k in legacy]
            if not mine and not listed:
                mine = [_prop_value(node, "Reference")]
            mine = list(dict.fromkeys(r for r in mine if r and not r.startswith("#") and "?" not in r))
            if mine:
                out.append(Sym(sh.path, node, mine, _prop_value(node, "Value"), _prop_value(node, "Footprint")))
    return out


def ref_of(node: sexpr.Node, sch: bool) -> list[str]:
    """Every reference a placed symbol (any instance listed) or a footprint names, hierarchy not checked."""
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
    nl = "\r\n" if text[max(line - 2, 0):line] == "\r\n" else "\n"
    return nl + lead if not lead.strip() else " "


def _set(text: str, node: sexpr.Node, name: str, value: str, board: bool, version: int,
         new_name: str | None = None, added: int = 0) -> tuple[str, str, list]:
    """(field name written, old value, edits) to make node's field read value; no edits when it already does.
    A field that isn't there yet is added as new_name (default: name); added counts the fields already added to this
    node in this call, so each gets its own (id N) in formats that number them."""
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
            new = re.sub(r"\(id\s+\d+\)", f"(id {top + 1 + added})", new, count=1)
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


def _key(name: str) -> str:
    """One key per field however it's spelled: the part-number kind (mpn, lcsc...), else the name itself."""
    return fab_export.field_kind(name) or name


def _expand(parts: dict) -> dict[str, dict[str, str]]:
    """{ref or "R1,R2,R5": {field: value}} -> {ref: {field: value}}, one entry per field (by _key)."""
    if not isinstance(parts, dict) or not parts:
        raise PartNumberError('parts must be {ref: {field: value}}, e.g. {"R1,R2": {"MPN": "...", "LCSC": "C..."}}')
    out: dict[str, dict[str, str]] = {}
    for key, fields in parts.items():
        if not isinstance(fields, dict) or not fields:
            raise PartNumberError(f"{key}: give the fields as {{name: value}}")
        clean: dict[str, str] = {}
        names: dict[str, str] = {}  # key -> the name it was given as
        for name, value in fields.items():
            name = str(name).strip()
            if not name:
                raise PartNumberError(f"{key}: a field with no name")
            if fab_export._norm(name) in DESIGN_FIELDS:
                raise PartNumberError(f"{key}: {name} is part of the design, not a part number; change it in KiCad")
            if value is not None and not isinstance(value, (str, int, float)):
                raise PartNumberError(f"{key}: {name} must be text, not {type(value).__name__}")
            value = "" if value is None else str(value).strip()
            if "\n" in value or "\r" in value:
                raise PartNumberError(f"{key}: {name} has a line break; a part number is one line")
            k = _key(name)
            if k in names:
                if clean[names[k]] != value:
                    raise PartNumberError(f"{key}: {names[k]} and {name} are the same field with different values; "
                                          "give it once")
                continue
            names[k] = name
            clean[name] = value
        for ref in (r.strip() for r in str(key).split(",")):
            if not ref:
                continue
            have = {_key(n): (n, v) for n, v in out.get(ref, {}).items()}
            for n, v in clean.items():
                if _key(n) in have and have[_key(n)][1] != v:
                    raise PartNumberError(f"{ref} is given twice with different values for {n}")
                if _key(n) not in have:
                    out.setdefault(ref, {})[n] = v
            out.setdefault(ref, {})
    return out


def set_part_numbers(schematic: str, parts: dict, board: str | None = None, dry_run: bool = False) -> dict:
    sch = Path(schematic).expanduser()
    if sch.suffix != ".kicad_sch" or not sch.exists():
        raise PartNumberError(f"no .kicad_sch at {sch}")
    want = _expand(parts)
    sheets = _sheet_files(sch)
    syms = _symbols(sheets)
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
        by_key: dict[str, tuple[str, str]] = {}  # key -> (name, value), the first ref's spelling
        bad = False
        for r in given:
            for n, v in want[r].items():
                if by_key.setdefault(_key(n), (n, v))[1] != v:
                    bad = True
        if bad:
            conflicts.append({"refs": given, "why": "these share one symbol (the same sheet used more than once), so "
                                                    "they can't have different numbers; nothing was set for them"})
            skip.update(s.refs)
            continue
        per_sym[id(s)] = dict(by_key.values())
        # every ref on it gets every field: say so when that's more than was asked for any of them
        if set(s.refs) - set(given) or any(len(want[r]) != len(by_key) for r in given):
            entry = {"refs": sorted(s.refs, key=fab_export._refkey), "given": sorted(given, key=fab_export._refkey),
                     "fields": sorted(n for n, _ in by_key.values())}
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
        added = 0
        for n, v in fields.items():
            written, old, edits = _set(sh.text, s.node, n, v, board=False, version=_version(sh.root),
                                       new_name=spelling(n), added=added)
            added += sum(1 for a, b, _ in edits if a == b)
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
    ref_fields = {r: per_sym[id(s)] for s in syms if id(s) in per_sym for r in s.refs}  # units share them
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
        edits, bchanged, missing = [], {}, []
        for r, fields in ref_fields.items():
            if r not in fps:
                missing.append(r)
                continue
            for fp in fps[r]:
                added = 0
                for n, v in fields.items():
                    # the board's own spelling if it has one, else the schematic's, as Update PCB would add it
                    written, old, e = _set(pcb_text, fp, n, v, board=True, version=_version(proot),
                                           new_name=written_as.get(r, {}).get(n, n), added=added)
                    added += sum(1 for a, b, _ in e if a == b)
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
        if f in sheet_of:
            nodes = [(r, s.node) for s in _symbols([Sheet(f, text, new, sheet_of[f].paths)]) for r in s.refs]
        else:
            nodes = [(_prop_value(n, "Reference"), n) for n in new.children("footprint")]
        for r, node in nodes:
            for n, v in ref_fields.get(r, {}).items():
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
        result["unknown_note"] = "not parts of the schematic, so nothing was set for them (a board-only part, such as " \
                                 "a fiducial or a mounting hole, isn't in the BOM)"
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

    # Eeschema locks only the root sheet while it has the hierarchy open, so check every file of the design
    locked = [lk.name for f in [sh.path for sh in sheets] + ([pcb] if pcb.exists() else []) for lk in [_lock(f)] if lk]
    if locked:
        raise PartNumberError(f"KiCad has this design open ({', '.join(locked)}), so its next save would undo this: "
                              "close the schematic and board in KiCad first (if KiCad isn't running, delete the stale "
                              ".lck file)")
    kept = project.keep([f for f, _ in writes], "part-numbers")
    # each file to a temporary one beside it first, then all moved into place, so a failure leaves no half file
    temps: list[tuple[Path, Path]] = []
    done: list[str] = []
    try:
        for f, text in writes:
            fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.name}.", suffix=".tmp")
            temps.append((Path(tmp), f))
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode("utf-8"))
            os.chmod(tmp, f.stat().st_mode & 0o7777)  # mkstemp makes it private; keep the file's own permissions
        for tmp, f in temps:
            os.replace(tmp, f)
            done.append(str(f))
    except OSError as e:
        for tmp, _ in temps:
            tmp.unlink(missing_ok=True)
        raise PartNumberError(f"writing failed ({e}); " + (f"already written: {', '.join(done)}; " if done else
                              "nothing was changed; ") + (f"the files as they were are in {kept}" if kept else "")) from None
    result["files_written"] = [str(f) for f, _ in writes]
    if kept:
        result["previous_kept"] = kept
    result["next"] = "check_ready_to_order to see what still has no number; export_fab puts the numbers in the BOM"
    return result
