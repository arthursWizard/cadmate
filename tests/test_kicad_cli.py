"""Run the kicad-cli commands cadmate uses against the real kicad-cli, so a release that renames a flag or changes the
report shape fails here instead of in front of a user. Skipped when kicad-cli isn't installed; CI runs them on Linux
with KiCad installed (see .github/workflows/tests.yml)."""

import os
import subprocess
from pathlib import Path

import pytest

from cadmate import apps, kicad_live, pcb_board, pcb_place, pcb_review, pcb_write, schematic_review, schematic_write, sexpr, versions
from test_pcb_review import HEAD, footprint, pad
from test_pictures import png_info
from test_schematic_write import LIBS, NETS, PARTS, big_symbol_lib, random_circuit

CLI = apps.find_kicad_cli()
FIXTURES = Path(__file__).parent / "fixtures"

if CLI is None and os.environ.get("CADMATE_REQUIRE_KICAD"):
    raise RuntimeError("CADMATE_REQUIRE_KICAD is set but kicad-cli wasn't found")  # CI: a skip would hide a broken install
pytestmark = pytest.mark.skipif(CLI is None, reason="kicad-cli not installed")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([CLI, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def test_installed_version_is_supported():
    info = apps.kicad_info().to_dict()
    print(info)  # shows the version in the CI log
    assert info["status"] in ("supported", "newer_than_tested"), info


def test_footprint_svg_flags(tmp_path):
    lib = tmp_path / "t.pretty"
    lib.mkdir()
    (lib / "SOIC-8.kicad_mod").write_bytes((FIXTURES / "SOIC-8.kicad_mod").read_bytes())
    p = run(*versions.kicad_cli_args("fp_svg"), "--footprint", "SOIC-8", "--output", str(tmp_path), str(lib))
    assert list(tmp_path.glob("*.svg")), p.stderr or p.stdout


def test_symbol_svg_flags(tmp_path):
    p = run(*versions.kicad_cli_args("sym_svg"), "--symbol", "NE555D", "--output", str(tmp_path),
            str(FIXTURES / "Symbols.kicad_symdir" / "NE555D.kicad_sym"))
    assert list(tmp_path.glob("*.svg")), p.stderr or p.stdout


@pytest.mark.parametrize("item, name", [("SOIC-8.kicad_mod", None),
                                        ("Symbols.kicad_symdir/NE555D.kicad_sym", "NE555D")])
def test_snapshot_png(tmp_path, item, name):
    src = FIXTURES / item
    path = tmp_path / src.name
    path.write_bytes(src.read_bytes())
    png = kicad_live.snapshot(str(path), name, width=600)
    width, height, corner = png_info(png)
    assert width == 600 and height > 50 and corner[:3] == (255, 255, 255)
    assert png.stat().st_size > 2_000  # a drawing, not a blank page


def test_drc_report_has_the_fields_cadmate_reads(tmp_path):
    part = footprint("Resistor_SMD:R_0603", "R1", "1k", 20, 20, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")])
    other = footprint("Resistor_SMD:R_0603", "R2", "1k", 40, 20, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")])
    f = tmp_path / "t.kicad_pcb"
    f.write_text(HEAD + part + other + ")\n", encoding="utf-8", newline="\n")
    rep = pcb_review.run_drc(f)
    assert rep is not None and "error" not in rep, rep
    assert {"violations", "unconnected_items"} <= set(rep), sorted(rep)
    assert rep["unconnected_items"], "A and B are unrouted between R1 and R2"  # what _drc_findings counts
    item = rep["unconnected_items"][0]["items"][0]
    assert "description" in item and {"x", "y"} <= set(item["pos"])


def test_pcb_svg_flags(tmp_path):
    f = tmp_path / "t.kicad_pcb"
    f.write_text(HEAD + ")\n", encoding="utf-8", newline="\n")
    svg = tmp_path / "t.svg"
    p = run(*versions.kicad_cli_args("pcb_svg"), "--layers", "F.Cu,Edge.Cuts", "--output", str(svg), str(f))
    assert svg.exists(), p.stderr or p.stdout


@pytest.mark.parametrize("side", ["top", "bottom"])
def test_assembly_drawing_pdf_flags(tmp_path, side):
    part = footprint("Resistor_SMD:R_0603", "R1", "1k", 20, 20, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")])
    f = tmp_path / "t.kicad_pcb"
    f.write_text(HEAD + part + ")\n", encoding="utf-8", newline="\n")
    pdf = tmp_path / f"t-{side}.pdf"
    layers = "F.Fab,Edge.Cuts" if side == "top" else "B.Fab,Edge.Cuts"
    mirror = ["--mirror"] if side == "bottom" else []
    p = run(*versions.kicad_cli_args("pcb_pdf"), *mirror, "--layers", layers, "--output", str(pdf), str(f))
    assert pdf.exists() and pdf.read_bytes().startswith(b"%PDF"), p.stderr or p.stdout


def test_version_output_parses():
    p = run(*versions.kicad_cli_args("version"))
    assert versions.parse(p.stdout) is not None, p.stdout


def test_board_step_flags(tmp_path):
    part = footprint("Resistor_SMD:R_0603", "R1", "1k", 20, 20, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")])
    f = tmp_path / "t.kicad_pcb"
    f.write_text(HEAD + part + ")\n", encoding="utf-8", newline="\n")
    step = tmp_path / "t.step"
    p = run(*versions.kicad_cli_args("pcb_step"), "--output", str(step), str(f))
    assert step.exists() and step.read_text(encoding="utf-8", errors="replace").startswith("ISO-10303-21"), p.stderr or p.stdout


def test_board_step_with_a_model_that_isnt_there(tmp_path, monkeypatch):
    """A model link kicad-cli can't resolve: the export still succeeds, and export_step names the part."""
    from cadmate import fab_export

    part = footprint("Resistor_SMD:R_0603", "R1", "1k", 20, 20, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")])
    part = part.replace("(attr smd)", '(attr smd) (model "${KIPRJMOD}/models/gone.step")')
    f = tmp_path / "t.kicad_pcb"
    f.write_text(HEAD + part + ")\n", encoding="utf-8", newline="\n")
    p = run(*versions.kicad_cli_args("pcb_step"), "--output", str(tmp_path / "raw.step"), str(f))
    print(p.returncode, p.stdout, p.stderr)  # what kicad-cli says about it, in the CI log
    r = fab_export.export_step(f, out=tmp_path / "t.step")
    assert "R1" in r["model_not_loaded"], r


def kicad_nets(sch: Path) -> list[set[str]]:
    """KiCad's own netlist for a sheet: each net's pins as REF.PIN."""
    net = sch.with_suffix(".net")
    p = run("sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(net), str(sch))
    assert net.exists(), p.stderr or p.stdout
    return [{f"{x.child('ref').atoms()[0]}.{x.child('pin').atoms()[0]}" for x in n.children("node")}
            for n in sexpr.parse(net.read_text(encoding="utf-8")).child("nets").children("net")]


def erc_errors(sch: Path) -> list:
    rep = schematic_review.run_erc(sch)
    assert rep and "error" not in rep, rep
    return [(v["type"], v["description"]) for s in rep.get("sheets", []) for v in s.get("violations", [])
            if v.get("severity") == "error"]


def test_a_written_schematic_loads_and_passes_erc(tmp_path):
    """KiCad reads the sheet cadmate writes (format, lib_symbols, instances), ERC finds no errors, and KiCad's own
    netlist joins exactly the pins that were asked for."""
    out = tmp_path / "demo.kicad_sch"
    schematic_write.write(str(out), PARTS, NETS, no_connect=["U1.3", "U1.4", "U1.6", "U1.8"], libraries=LIBS, erc=False)
    assert not erc_errors(out)
    got = kicad_nets(out)
    want = [{"U1.1", "C1.1", "R1.1"}, {"U1.2", "C1.2", "R3.2"}, {"U1.7", "R1.2"}, {"U1.5", "R2.1"}, {"R2.2", "R3.1"}]
    for pins in want:
        assert pins in got, (pins, got)


@pytest.mark.parametrize("seed", [2, 49, 82])
def test_bigger_written_schematics_have_the_nets_asked_for(tmp_path, seed):
    """A 40-pin chip with up to 25 parts across blocks, by KiCad's own netlist."""
    parts, nets, nc = random_circuit(seed)
    out = tmp_path / "big.kicad_sch"
    schematic_write.write(str(out), parts, nets, no_connect=nc, libraries={**LIBS, "Big": big_symbol_lib(tmp_path)},
                          paper="A3", erc=False)
    got = [g for g in kicad_nets(out) if len(g) > 1]
    want = [set(p) for p in nets.values() if len(p) > 1]
    assert sorted(map(sorted, got)) == sorted(map(sorted, want))


def test_a_board_from_a_written_schematic_matches_it(tmp_path):
    """Schematic to board: KiCad loads the board, its schematic parity check finds nothing, and every pad KiCad's
    netlist puts on a net carries that net."""
    fp = {"MCU8": "Fix:SOIC-8", "C": "Test:C_0603", "R": "Test:R_0603"}
    parts = [{**p, "footprint": fp[p["symbol"].split(":")[1]]} for p in PARTS]
    sch = tmp_path / "demo.kicad_sch"
    schematic_write.write(str(sch), parts, NETS, no_connect=["U1.3", "U1.4", "U1.6", "U1.8"], libraries=LIBS, erc=False)
    # R3 not fitted and kept off the BOM: the footprint must carry the same attributes or parity flags it
    text = sch.read_text(encoding="utf-8")
    r3 = next(n for n in sexpr.parse(text).children("symbol")
              if any(p.atoms()[:2] == ["Reference", "R3"] for p in n.children("property")))
    part = text[r3.start:r3.end].replace("(in_bom yes)", "(in_bom no)").replace("(dnp no)", "(dnp yes)")
    assert "(dnp yes)" in part and "(in_bom no)" in part
    sch.write_text(text[:r3.start] + part + text[r3.end:], encoding="utf-8")
    r = pcb_write.from_schematic(str(sch), libraries={"Test": str(FIXTURES / "pcb" / "Test.pretty"), "Fix": str(FIXTURES)})
    print(r)
    assert r["ok"] and r["footprints"] == 5
    assert r["parity"]["issues"] == 0, r["parity"]
    fp = next(f for f in sexpr.parse((tmp_path / "demo.kicad_pcb").read_text(encoding="utf-8")).children("footprint")
              if any(p.atoms()[:2] == ["Reference", "R3"] for p in f.children("property")))
    assert {"dnp", "exclude_from_bom"} <= set(fp.child("attr").atoms())
    b = pcb_board.parse((tmp_path / "demo.kicad_pcb").read_text(encoding="utf-8"))
    got = {f"{p.ref}.{pad.number}": pad.net for p in b.parts for pad in p.pads}
    for pins in kicad_nets(sch):
        if len(pins) > 1:
            assert len({got[p] for p in pins}) == 1 and None not in {got[p] for p in pins}, (pins, got)


def test_moved_and_turned_footprints_still_match_the_schematic(tmp_path):
    """apply_placement's file edit: KiCad loads the board after a move and a quarter turn, parity still finds nothing,
    and the turned pads land where cadmate says they do."""
    fp = {"MCU8": "Fix:SOIC-8", "C": "Test:C_0603", "R": "Test:R_0603"}
    parts = [{**p, "footprint": fp[p["symbol"].split(":")[1]]} for p in PARTS]
    sch = tmp_path / "demo.kicad_sch"
    schematic_write.write(str(sch), parts, NETS, no_connect=["U1.3", "U1.4", "U1.6", "U1.8"], libraries=LIBS, erc=False)
    pcb_write.from_schematic(str(sch), libraries={"Test": str(FIXTURES / "pcb" / "Test.pretty"), "Fix": str(FIXTURES)}, drc=False)
    src = tmp_path / "demo.kicad_pcb"
    b = pcb_board.parse(src.read_text(encoding="utf-8"))
    c = next(p for p in b.parts if p.ref.startswith("C"))
    out = tmp_path / "placed" / "demo.kicad_pcb"
    out.parent.mkdir()
    (out.parent / "demo.kicad_sch").write_text(sch.read_text(encoding="utf-8"), encoding="utf-8")
    to = {"x": round(b.bbox()[0] + 4, 3), "y": round(b.bbox()[1] + 4, 3), "rotation": (c.rotation + 90) % 360}
    r = pcb_place.apply(str(src), [{"ref": c.ref, "to": to}], out_path=str(out))
    assert r["moved"] == [c.ref]
    rep = pcb_review.run_drc(out)
    assert rep is not None
    s = pcb_review.drc_summary(rep)
    assert s["parity"] == 0, rep.get("schematic_parity")
    moved = pcb_board.parse(out.read_text(encoding="utf-8")).part(c.ref)
    assert (moved.x, moved.y, moved.rotation) == (to["x"], to["y"], to["rotation"])
    for old, new in zip(c.copper_pads, moved.copper_pads):
        assert new.angle == (old.angle + 90) % 360
