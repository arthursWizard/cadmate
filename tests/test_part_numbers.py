"""set_part_numbers: part numbers written onto the parts of a schematic someone drew, and onto the board beside it."""

import json
from pathlib import Path

import pytest

from cadmate import fab_export, project, sexpr
from cadmate import part_numbers as pn

ROOT_UUID = "a0000000-0000-4000-8000-000000000001"
SUB_A = "b0000000-0000-4000-8000-000000000001"
SUB_B = "b0000000-0000-4000-8000-000000000002"
MPN = "Mfr. Part Number"  # how the fixture's C1 spells it


def prop(name, value, at="100 50 0", hide=False):
    h = "\n\t\t\t(hide yes)" if hide else ""
    return (f'\t\t(property "{name}" "{value}"\n\t\t\t(at {at}){h}\n\t\t\t(effects\n\t\t\t\t(font\n'
            f"\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n")


def symbol(n, lib, value, fp, instances, unit=1, extra=()):
    """A placed symbol as KiCad 9 saves it. instances: [(path, ref)]."""
    paths = "".join(f'\t\t\t\t(path "{p}"\n\t\t\t\t\t(reference "{r}")\n\t\t\t\t\t(unit {unit})\n\t\t\t\t)\n'
                    for p, r in instances)
    return (f'\t(symbol\n\t\t(lib_id "{lib}")\n\t\t(at {100 + n * 10} 50 0)\n\t\t(unit {unit})\n'
            f"\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n"
            f'\t\t(uuid "c0000000-0000-4000-8000-{n:012d}")\n'
            + prop("Reference", instances[0][1]) + prop("Value", value)
            + prop("Footprint", fp, hide=True) + prop("Datasheet", "~", hide=True)
            + "".join(prop(k, v, hide=True) for k, v in extra)
            + f'\t\t(pin "1"\n\t\t\t(uuid "d0000000-0000-4000-8000-{n:012d}")\n\t\t)\n'
            + f'\t\t(instances\n\t\t\t(project "main"\n{paths}\t\t\t)\n\t\t)\n\t)\n')


def sheet(uuid, body):
    return (f'(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "9.0")\n'
            f'\t(uuid "{uuid}")\n\t(paper "A4")\n\t(lib_symbols)\n{body})\n')


def sym_uuid(n):
    return f"c0000000-0000-4000-8000-{n:012d}"


def footprint(ref, name, fields=(), path=None, value=None):
    """A footprint as KiCad 9 saves it; path links it to its symbol (/<sheet uuid>/.../<symbol uuid>)."""
    value = name if value is None else value
    def p(name, val, hide=True):
        h = "\n\t\t\t(hide yes)" if hide else ""
        return (f'\t\t(property "{name}" "{val}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab"){h}\n'
                f'\t\t\t(uuid "e0000000-0000-4000-8000-{abs(hash((ref, name))) % 10**12:012d}")\n'
                f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n")
    link = f'\t\t(path "{path}")\n' if path else ""
    return (f'\t(footprint "Lib:{name}"\n\t\t(layer "F.Cu")\n\t\t(at 10 10)\n' + p("Reference", ref, False)
            + p("Value", value) + p("Footprint", f"Lib:{name}") + "".join(p(k, v) for k, v in fields) + link + "\t)\n")


@pytest.fixture
def design(tmp_path):
    """main.kicad_sch with R1, C1 (an existing "Mfr. Part Number"), a two-unit U1 and a sub-sheet used twice, whose
    one resistor is R10 in one use and R110 in the other; main.kicad_pcb has every part but R110."""
    d = tmp_path / "kicad"
    d.mkdir()
    top = "/" + ROOT_UUID
    body = (symbol(1, "Device:R", "10k", "Resistor_SMD:R_0603", [(top, "R1")])
            + symbol(2, "Device:C", "100n", "Capacitor_SMD:C_0603", [(top, "C1")],
                     extra=[("Mfr. Part Number", "OLD-123"), ("LCSC Part #", "")])
            + symbol(3, "Amp:Dual", "LM358", "Package_SO:SOIC-8", [(top, "U1")], unit=1)
            + symbol(4, "Amp:Dual", "LM358", "Package_SO:SOIC-8", [(top, "U1")], unit=2)
            + symbol(5, "power:GND", "GND", "", [(top, "#PWR01")])
            + "".join(f'\t(sheet\n\t\t(at 50 100)\n\t\t(size 20 10)\n\t\t(uuid "{u}")\n'
                      f'\t\t(property "Sheetname" "{n}"\n\t\t\t(at 50 99 0)\n\t\t)\n'
                      f'\t\t(property "Sheetfile" "sub.kicad_sch"\n\t\t\t(at 50 111 0)\n\t\t)\n\t)\n'
                      for u, n in ((SUB_A, "a"), (SUB_B, "b"))))
    (d / "main.kicad_sch").write_text(sheet(ROOT_UUID, body), encoding="utf-8", newline="\n")
    sub = symbol(6, "Device:R", "1k", "Resistor_SMD:R_0603", [(f"{top}/{SUB_A}", "R10"), (f"{top}/{SUB_B}", "R110")])
    (d / "sub.kicad_sch").write_text(sheet("f0000000-0000-4000-8000-000000000001", sub), encoding="utf-8", newline="\n")
    pcb = ('(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
           + footprint("R1", "R_0603", path=f"/{sym_uuid(1)}", value="10k")
           + footprint("C1", "C_0603", [("Mfr. Part Number", "OLD-123")], path=f"/{sym_uuid(2)}", value="100n")
           + footprint("U1", "SOIC-8", path=f"/{sym_uuid(3)}", value="LM358")
           + footprint("R10", "R_0603", path=f"/{SUB_A}/{sym_uuid(6)}", value="1k") + ")\n")
    (d / "main.kicad_pcb").write_text(pcb, encoding="utf-8", newline="\n")
    return d


def fields(path, ref):
    root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    for n in root.children("symbol") + root.children("footprint"):
        if ref in pn.ref_of(n, n.head == "symbol"):
            yield {p.atoms()[0]: (p.atoms()[1:2] or [""])[0] for p in n.children("property")}


def test_numbers_go_onto_a_drawn_sheet_and_nothing_else_moves(design):
    sch = design / "main.kicad_sch"
    before = sch.read_text(encoding="utf-8")
    r = pn.set_part_numbers(str(sch), {"R1": {"Manufacturer": "Yageo", "MPN": "RC0603FR-0710KL", "LCSC": "C98220"}})
    assert r["ok"] and set(r["changed"]) == {"R1"}
    # new fields take the spelling the sheet already uses (C1's), else cadmate's
    assert r["changed"]["R1"]["value"] == "10k"
    assert r["changed"]["R1"]["fields"][MPN] == {"from": "", "to": "RC0603FR-0710KL"}
    after = sch.read_text(encoding="utf-8")
    (f,) = fields(sch, "R1")
    assert f[MPN] == "RC0603FR-0710KL" and f["LCSC Part #"] == "C98220" and f["Manufacturer"] == "Yageo"
    # only the new fields were added: take them out again and the file is what it was, byte for byte
    added = [after[n.start:n.end] for s in sexpr.parse(after).children("symbol") if "R1" in pn.ref_of(s, True)
             for n in s.children("property") if n.atoms()[0] in (MPN, "LCSC Part #", "Manufacturer")]
    assert len(added) == 3
    stripped = after
    for a in added:
        i = stripped.index(a)
        j = stripped.rfind("\n", 0, i)
        stripped = stripped[:j] + stripped[i + len(a):]
    assert stripped == before
    # each new field is a hidden copy of the symbol's own formatting
    assert all("(hide yes)" in a and "(size 1.27 1.27)" in a for a in added)
    assert r["board"]["changed"]["R1"]["LCSC Part #"] == {"from": "", "to": "C98220"}
    bom = {p["ref"]: p for p in fab_export.footprints((design / "main.kicad_pcb").read_text(encoding="utf-8"))}
    assert bom["R1"]["mpn"] == "RC0603FR-0710KL" and bom["R1"]["lcsc"] == "C98220" and bom["R1"]["manufacturer"] == "Yageo"


def test_a_field_spelled_another_way_is_updated_where_it_is(design):
    sch = design / "main.kicad_sch"
    r = pn.set_part_numbers(str(sch), {"C1": {"MPN": "CL10B104KB8NNNC", "lcsc": "C1591"}})
    assert r["changed"]["C1"]["fields"] == {"Mfr. Part Number": {"from": "OLD-123", "to": "CL10B104KB8NNNC"},
                                            "LCSC Part #": {"from": "", "to": "C1591"}}
    (f,) = fields(sch, "C1")
    assert "MPN" not in f and "LCSC" not in f and f["Mfr. Part Number"] == "CL10B104KB8NNNC"
    (b,) = fields(design / "main.kicad_pcb", "C1")
    assert b["Mfr. Part Number"] == "CL10B104KB8NNNC" and b["LCSC Part #"] == "C1591"
    # the same again changes nothing and writes nothing
    again = pn.set_part_numbers(str(sch), {"C1": {"MPN": "CL10B104KB8NNNC", "lcsc": "C1591"}})
    assert again["changed"] == {} and again["already_set"] == ["C1"] and again["files_written"] == []


def test_every_unit_of_a_part_gets_the_numbers(design):
    sch = design / "main.kicad_sch"
    pn.set_part_numbers(str(sch), {"U1": {"MPN": "LM358DR"}})
    units = list(fields(sch, "U1"))
    assert len(units) == 2 and all(u[MPN] == "LM358DR" for u in units)
    assert sum(1 for _ in fields(design / "main.kicad_pcb", "U1")) == 1
    assert (design / "main.kicad_pcb").read_text(encoding="utf-8").count('"LM358DR"') == 1


def test_a_sheet_used_twice_shares_its_numbers(design):
    sch = design / "main.kicad_sch"
    r = pn.set_part_numbers(str(sch), {"R10": {"MPN": "RC0603FR-071KL"}})
    assert set(r["changed"]) == {"R10", "R110"}
    assert r["shared_symbols"] == [{"refs": ["R10", "R110"], "given": ["R10"], "fields": ["MPN"]}]
    assert r["board"]["not_on_board"] == ["R110"]
    assert str(design / "sub.kicad_sch") in r["files_written"]
    (f,) = fields(design / "sub.kicad_sch", "R110")
    assert f[MPN] == "RC0603FR-071KL"

    r = pn.set_part_numbers(str(sch), {"R10": {"MPN": "A"}, "R110": {"MPN": "B"}, "R1": {"MPN": "C"}})
    assert r["conflicts"][0]["refs"] == ["R10", "R110"] and set(r["changed"]) == {"R1"}
    (f,) = fields(design / "sub.kicad_sch", "R10")
    assert f[MPN] == "RC0603FR-071KL"


def test_mistakes_are_reported_or_refused(design):
    sch = design / "main.kicad_sch"
    before = {f: f.read_bytes() for f in design.iterdir()}
    with pytest.raises(pn.PartNumberError, match="part of the design"):
        pn.set_part_numbers(str(sch), {"R1": {"Value": "22k"}})
    with pytest.raises(pn.PartNumberError, match="given twice"):
        pn.set_part_numbers(str(sch), {"R1": {"MPN": "A"}, "R1,C1": {"MPN": "B"}})
    with pytest.raises(pn.PartNumberError, match="one line"):
        pn.set_part_numbers(str(sch), {"R1": {"MPN": "A\rB"}})
    with pytest.raises(pn.PartNumberError, match="must be text"):
        pn.set_part_numbers(str(sch), {"R1": {"MPN": ["A"]}})
    r = pn.set_part_numbers(str(sch), {"R1,C1": {"LCSC": "98220"}, "R99": {"MPN": "X"}}, dry_run=True)
    assert r["unknown_refs"] == ["R99"] and set(r["changed"]) == {"C1", "R1"} and r["files_written"] == []
    assert any("isn't C followed by digits" in w for w in r["warnings"])
    assert any("different values" in w for w in r["warnings"])
    assert {f: f.read_bytes() for f in design.iterdir()} == before  # a dry run writes nothing

    (design / "~main.kicad_sch.lck").write_text("{}")
    with pytest.raises(pn.PartNumberError, match="open"):
        pn.set_part_numbers(str(sch), {"R1": {"MPN": "X"}})
    (design / "~main.kicad_sch.lck").unlink()


def test_the_old_files_are_kept_first(design, tmp_path):
    # outside a project: a .bak beside each file written
    pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "X"}})
    assert (design / "main.kicad_sch.bak").exists() and (design / "main.kicad_pcb.bak").exists()
    assert not (design / "sub.kicad_sch.bak").exists()
    assert '"X"' not in (design / "main.kicad_sch.bak").read_text(encoding="utf-8")

    # inside one: a snapshot in history/
    root = tmp_path / "proj"
    project.init(root, tools=["kicad"])
    (root / "kicad").mkdir(exist_ok=True)
    for f in ("main.kicad_sch", "sub.kicad_sch", "main.kicad_pcb"):
        (root / "kicad" / f).write_bytes((design / f).read_bytes())
    r = pn.set_part_numbers(str(root / "kicad" / "main.kicad_sch"), {"R1": {"MPN": "Y"}})
    meta = json.loads((Path(r["previous_kept"]) / "SNAPSHOT.json").read_text())
    assert sorted(meta["paths"]) == ["kicad/main.kicad_pcb", "kicad/main.kicad_sch"]


def test_older_kicad_formats(tmp_path):
    """KiCad 7: numbered fields with hide inside effects, and board properties as plain (property name value)."""
    sch = tmp_path / "old.kicad_sch"
    sch.write_text(
        '(kicad_sch (version 20230121) (generator eeschema)\n  (uuid 1)\n'
        '  (symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (in_bom yes) (on_board yes) (uuid 2)\n'
        '    (property "Reference" "R1" (at 10 9 0) (effects (font (size 1.27 1.27))))\n'
        '    (property "Value" "1k" (at 10 11 0) (effects (font (size 1.27 1.27))))\n'
        '    (property "Footprint" "R_0603" (at 10 10 0) (effects (font (size 1.27 1.27)) hide))\n'
        '    (instances (project "old" (path "/1" (reference "R1") (unit 1))))\n  )\n)\n', encoding="utf-8")
    sch.with_suffix(".kicad_pcb").write_text(
        '(kicad_pcb (version 20221018) (generator pcbnew)\n'
        '  (footprint "Lib:R_0603" (layer "F.Cu") (at 1 1)\n    (property "Sheetfile" "old.kicad_sch")\n'
        '    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))\n  )\n)\n', encoding="utf-8")
    pn.set_part_numbers(str(sch), {"R1": {"MPN": "X"}})
    text = sch.read_text(encoding="utf-8")
    assert '(property "MPN" "X" (at 10 10 0) (effects (font (size 1.27 1.27)) hide))' in text
    # the board names its parts in fp_text, which this doesn't read: it says so rather than guessing
    r = pn.set_part_numbers(str(sch), {"R1": {"LCSC": "C1"}})
    assert r["board"]["not_on_board"] == ["R1"]


def test_the_whole_design_is_checked_for_kicad_having_it_open(design):
    """Eeschema locks only the root sheet, so a part on a sub-sheet is refused too, and nothing is written."""
    before = {f: f.read_bytes() for f in design.iterdir()}
    (design / "~main.kicad_sch.lck").write_text("{}")
    with pytest.raises(pn.PartNumberError, match="open"):
        pn.set_part_numbers(str(design / "main.kicad_sch"), {"R10": {"MPN": "X"}})
    (design / "~main.kicad_sch.lck").unlink()
    (design / "~main.kicad_pcb.lck").write_text("{}")
    with pytest.raises(pn.PartNumberError, match="open"):
        pn.set_part_numbers(str(design / "main.kicad_sch"), {"R10": {"MPN": "X"}})
    (design / "~main.kicad_pcb.lck").unlink()
    assert {f: f.read_bytes() for f in design.iterdir()} == before


def test_one_field_under_two_spellings(design):
    sch = design / "main.kicad_sch"
    with pytest.raises(pn.PartNumberError, match="same field with different values"):
        pn.set_part_numbers(str(sch), {"U1": {"DigiKey": "A", "Digi-Key PN": "B"}})
    with pytest.raises(pn.PartNumberError, match="given twice"):
        pn.set_part_numbers(str(sch), {"U1": {"DigiKey": "A"}, "U1,R1": {"Digi-Key PN": "B"}})
    r = pn.set_part_numbers(str(sch), {"U1": {"DigiKey": "A", "Digi-Key PN": "A"}})
    assert r["board"]["changed"]["U1"] == {"DigiKey": {"from": "", "to": "A"}}
    assert (design / "main.kicad_pcb").read_text(encoding="utf-8").count('"DigiKey"') == 1
    # across refs sharing a symbol: one field, however each spelled it
    r = pn.set_part_numbers(str(sch), {"R10": {"lcsc": "C5"}, "R110": {"LCSC": "C5"}})
    assert set(r["changed"]) == {"R10", "R110"} and "conflicts" not in r
    assert (design / "sub.kicad_sch").read_text(encoding="utf-8").count('"C5"') == 1
    r = pn.set_part_numbers(str(sch), {"R10": {"lcsc": "C5"}, "R110": {"LCSC": "C6"}})
    assert r["conflicts"][0]["refs"] == ["R10", "R110"] and r["files_written"] == []


def test_refs_from_sheets_no_longer_in_the_hierarchy_are_ignored(design):
    """A symbol keeps instance paths of sheets since deleted; a reference there may now be another part's."""
    sub = design / "sub.kicad_sch"
    stale = '\t\t\t\t(path "/a0000000-0000-4000-8000-000000000001/deadbeef-0000-4000-8000-000000000000"\n' \
            '\t\t\t\t\t(reference "R1")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n'
    text = sub.read_text(encoding="utf-8")
    i = text.index("\t\t\t\t(path")
    sub.write_text(text[:i] + stale + text[i:], encoding="utf-8", newline="\n")
    before = sub.read_bytes()
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "X"}})
    assert set(r["changed"]) == {"R1"} and "shared_symbols" not in r
    assert sub.read_bytes() == before


def test_refs_sharing_a_symbol_with_different_fields_are_reported(design):
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"R10": {"Mouser": "M1"}, "R110": {"MPN": "P1"}})
    assert r["shared_symbols"] == [{"refs": ["R10", "R110"], "given": ["R10", "R110"], "fields": ["MPN", "Mouser"]}]
    assert r["changed"]["R10"]["fields"].keys() == {"Mouser", MPN}


def test_a_board_only_part_is_reported_not_set(design):
    pcb = design / "main.kicad_pcb"
    pcb.write_text(pcb.read_text(encoding="utf-8").replace("\t(footprint", footprint("H1", "Fiducial") + "\t(footprint", 1),
                   encoding="utf-8", newline="\n")
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"H1": {"MPN": "X"}, "R1": {"MPN": "Y"}})
    assert r["unknown_refs"] == ["H1"] and set(r["changed"]) == {"R1"}
    (h1,) = fields(pcb, "H1")
    assert MPN not in h1 and "MPN" not in h1


def test_line_endings_and_numbered_fields_are_kept(tmp_path):
    sch = tmp_path / "old.kicad_sch"
    sch.write_bytes((
        '(kicad_sch (version 20230121) (generator eeschema)\n  (uuid 1)\n'
        '  (symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (in_bom yes) (on_board yes) (uuid 2)\n'
        '    (property "Reference" "R1" (at 10 9 0) (id 0) (effects (font (size 1.27 1.27))))\n'
        '    (property "Value" "1k" (at 10 11 0) (id 1) (effects (font (size 1.27 1.27))))\n'
        '    (property "Footprint" "R_0603" (at 10 10 0) (id 2) (effects (font (size 1.27 1.27)) hide))\n'
        '    (instances (project "old" (path "/1" (reference "R1") (unit 1))))\n  )\n)\n').replace("\n", "\r\n").encode())
    pn.set_part_numbers(str(sch), {"R1": {"MPN": "X", "LCSC": "C1", "Mouser": "M"}})
    raw = sch.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert [int(i) for i in __import__("re").findall(rb"\(id (\d+)\)", raw)] == [0, 1, 2, 3, 4, 5]


def test_a_kicad6_hierarchy_used_twice(tmp_path):
    """KiCad 6 keeps every reference in the root's symbol_instances, by paths without the root's uuid."""
    def sym(uuid, ref, x):
        return (f'  (symbol (lib_id "Device:R") (at {x} 50 0) (unit 1) (in_bom yes) (on_board yes) (uuid {uuid})\n'
                f'    (property "Reference" "{ref}" (id 0) (at {x} 49 0) (effects (font (size 1.27 1.27))))\n'
                f'    (property "Value" "1k" (id 1) (at {x} 51 0) (effects (font (size 1.27 1.27))))\n'
                f'    (property "Footprint" "R_0603" (id 2) (at {x} 50 0) (effects (font (size 1.27 1.27)) hide))\n  )\n')
    sheets = "".join(f'  (sheet (at 50 100) (size 20 10) (uuid {u})\n    (property "Sheet name" "{n}" (id 0) (at 50 99 0))\n'
                     f'    (property "Sheet file" "sub.kicad_sch" (id 1) (at 50 111 0))\n  )\n'
                     for u, n in (("11111111-0000-0000-0000-00000000000a", "a"), ("11111111-0000-0000-0000-00000000000b", "b")))
    inst = ('  (sheet_instances (path "/" (page "1")) (path "/11111111-0000-0000-0000-00000000000a" (page "2"))\n'
            '    (path "/11111111-0000-0000-0000-00000000000b" (page "3")))\n'
            '  (symbol_instances\n'
            '    (path "/22222222-0000-0000-0000-000000000001" (reference "R1") (unit 1) (value "1k") (footprint "R_0603"))\n'
            '    (path "/11111111-0000-0000-0000-00000000000a/22222222-0000-0000-0000-000000000002" (reference "R10") (unit 1))\n'
            '    (path "/11111111-0000-0000-0000-00000000000b/22222222-0000-0000-0000-000000000002" (reference "R110") (unit 1))\n'
            '  )\n')
    (tmp_path / "main.kicad_sch").write_text('(kicad_sch (version 20211123) (generator eeschema)\n'
                                             '  (uuid 33333333-0000-0000-0000-000000000000)\n'
                                             + sym("22222222-0000-0000-0000-000000000001", "R1", 10) + sheets + inst + ")\n")
    (tmp_path / "sub.kicad_sch").write_text('(kicad_sch (version 20211123) (generator eeschema)\n'
                                            '  (uuid 44444444-0000-0000-0000-000000000000)\n'
                                            + sym("22222222-0000-0000-0000-000000000002", "R10", 20) + ")\n")
    r = pn.set_part_numbers(str(tmp_path / "main.kicad_sch"), {"R110": {"MPN": "X"}})
    assert "unknown_refs" not in r and set(r["changed"]) == {"R10", "R110"}
    assert r["shared_symbols"] == [{"refs": ["R10", "R110"], "given": ["R110"], "fields": ["MPN"]}]
    r = pn.set_part_numbers(str(tmp_path / "main.kicad_sch"), {"R1": {"MPN": "Y"}})
    assert set(r["changed"]) == {"R1"}


def test_an_out_of_date_board_is_left_alone(design):
    """After re-annotation, before Update PCB, the board's "R1" is still the old part: match footprints by the symbol
    they're linked to, not by reference text."""
    pcb = design / "main.kicad_pcb"
    text = pcb.read_text(encoding="utf-8")
    # R1 and C1 swapped on the board only, as after re-annotating the schematic
    swapped = text.replace('"Reference" "R1"', '"Reference" "TMP"').replace('"Reference" "C1"', '"Reference" "R1"') \
                  .replace('"Reference" "TMP"', '"Reference" "C1"')
    pcb.write_text(swapped, encoding="utf-8", newline="\n")
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "RES-1"}})
    assert r["board"]["out_of_date"] == ["R1"] and r["board"]["changed"] == {}
    assert pcb.read_text(encoding="utf-8") == swapped  # C1's footprint kept its OLD-123
    (f,) = fields(design / "main.kicad_sch", "R1")
    assert f[MPN] == "RES-1"


def test_a_footprint_without_a_link_counts_only_when_its_value_agrees(design):
    pcb = design / "main.kicad_pcb"
    text = pcb.read_text(encoding="utf-8")
    unlinked = text.replace(f'\t\t(path "/{sym_uuid(1)}")\n', "").replace(f'\t\t(path "/{sym_uuid(2)}")\n', "")
    unlinked = unlinked.replace('"Value" "100n"', '"Value" "220n"')
    pcb.write_text(unlinked, encoding="utf-8", newline="\n")
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "A"}, "C1": {"MPN": "B"}})
    assert set(r["board"]["changed"]) == {"R1"} and r["board"]["out_of_date"] == ["C1"]


def test_a_linked_sheet_is_edited_where_it_lives(design, tmp_path):
    shared = tmp_path / "lib"
    shared.mkdir()
    (design / "sub.kicad_sch").rename(shared / "sub.kicad_sch")
    (design / "sub.kicad_sch").symlink_to(shared / "sub.kicad_sch")
    pn.set_part_numbers(str(design / "main.kicad_sch"), {"R10": {"MPN": "X"}})
    assert (design / "sub.kicad_sch").is_symlink()
    (f,) = fields(shared / "sub.kicad_sch", "R10")
    assert f[MPN] == "X"


def test_a_file_saved_meanwhile_is_not_written_over(design, monkeypatch):
    real = pn.project.keep

    def save_first(files, why):  # KiCad saves the board between the read and the write
        p = design / "main.kicad_pcb"
        p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return real(files, why)
    monkeypatch.setattr(pn.project, "keep", save_first)
    before = (design / "main.kicad_sch").read_bytes()
    with pytest.raises(pn.PartNumberError, match="changed while this ran"):
        pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "X"}})
    assert (design / "main.kicad_sch").read_bytes() == before


def test_an_unlinked_footprint_must_be_the_symbols_footprint_too(design):
    """Without a link, the reference and value can agree by chance (a 10k swapped for another 10k in another
    package); the footprint name has to agree as well. The library nickname may differ."""
    pcb = design / "main.kicad_pcb"
    text = pcb.read_text(encoding="utf-8").replace(f'\t\t(path "/{sym_uuid(1)}")\n', "")
    pcb.write_text(text.replace('(footprint "Lib:R_0603"', '(footprint "Lib:R_0805"', 1), encoding="utf-8", newline="\n")
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "A"}})
    assert r["board"]["out_of_date"] == ["R1"] and r["board"]["changed"] == {}
    pcb.write_text(text, encoding="utf-8", newline="\n")  # Lib:R_0603 against the symbol's Resistor_SMD:R_0603
    r = pn.set_part_numbers(str(design / "main.kicad_sch"), {"R1": {"MPN": "A"}})
    assert set(r["board"]["changed"]) == {"R1"}


def test_a_part_whose_footprint_still_has_its_old_reference_is_out_of_date(design):
    """Re-annotated R1 -> R5 in the schematic, board not updated: R5's footprint is there, still reading R1."""
    sch = design / "main.kicad_sch"
    sch.write_text(sch.read_text(encoding="utf-8").replace('"R1"', '"R5"'), encoding="utf-8", newline="\n")
    r = pn.set_part_numbers(str(sch), {"R5": {"MPN": "A"}})
    assert r["board"]["out_of_date"] == ["R5"] and "not_on_board" not in r["board"]
    assert "parity" in r["board"]["out_of_date_note"]


def test_unlinked_footprints_that_could_be_each_other_are_left_alone(design):
    """Two footprints with no link to their symbols, the same value and the same footprint name: nothing on the board
    says which is which (the schematic may have been re-annotated since), so they're written only when both would
    get the same numbers, and otherwise left alone and named."""
    sch, pcb = design / "main.kicad_sch", design / "main.kicad_pcb"
    top = "/" + ROOT_UUID
    text = sch.read_text(encoding="utf-8")
    i = text.index("\t(sheet\n")
    sch.write_text(text[:i] + symbol(7, "Device:R", "10k", "Resistor_SMD:R_0603", [(top, "R2")]) + text[i:],
                   encoding="utf-8", newline="\n")
    board = pcb.read_text(encoding="utf-8").replace(f'\t\t(path "/{sym_uuid(1)}")\n', "")
    board = board[:board.rindex(")")] + footprint("R2", "R_0603", value="10k") + ")\n"
    pcb.write_text(board, encoding="utf-8", newline="\n")

    r = pn.set_part_numbers(str(sch), {"R1": {"MPN": "A"}, "R2": {"MPN": "B"}})
    assert r["board"]["changed"] == {} and pcb.read_text(encoding="utf-8") == board
    assert r["board"]["ambiguous"] == [{"refs": ["R1", "R2"], "value": "10k", "footprint": "R_0603"}]
    assert "Update PCB from Schematic" in r["board"]["ambiguous_note"]
    assert [f[MPN] for f in fields(sch, "R1")] == ["A"] and [f[MPN] for f in fields(sch, "R2")] == ["B"]

    # R2 now has B in the schematic: R1 alone getting B leaves both the same, so it doesn't matter which is which
    r = pn.set_part_numbers(str(sch), {"R1": {"MPN": "B"}})
    assert set(r["board"]["changed"]) == {"R1"} and "ambiguous" not in r["board"]
    # one of them asked for alone, the other different: left alone
    r = pn.set_part_numbers(str(sch), {"R1": {"MPN": "C"}})
    assert r["board"]["changed"] == {} and r["board"]["ambiguous"][0]["refs"] == ["R1", "R2"]
    # the same numbers for both: written, whichever is which
    r = pn.set_part_numbers(str(sch), {"R1,R2": {"MPN": "D", "LCSC": "C25804"}})
    assert set(r["board"]["changed"]) == {"R1", "R2"} and "ambiguous" not in r["board"]
    assert [f[MPN] for f in fields(pcb, "R1")] == ["D"] and [f[MPN] for f in fields(pcb, "R2")] == ["D"]
