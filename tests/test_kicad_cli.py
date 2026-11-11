"""Run the kicad-cli commands cadmate uses against the real kicad-cli, so a release that renames a flag or changes the
report shape fails here instead of in front of a user. Skipped when kicad-cli isn't installed; CI runs them on Linux
with KiCad installed (see .github/workflows/tests.yml)."""

import os
import subprocess
from pathlib import Path

import pytest
from test_pcb_review import HEAD, footprint, pad
from test_schematic_write import LIBS, NETS, PARTS

from cadmate import apps, pcb_review, schematic_review, schematic_write, sexpr, versions

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


def test_a_written_schematic_loads_and_passes_erc(tmp_path):
    """KiCad reads the sheet cadmate writes (format, lib_symbols, instances), ERC finds no errors, and KiCad's own
    netlist joins exactly the pins that were asked for."""
    out = tmp_path / "demo.kicad_sch"
    schematic_write.write(str(out), PARTS, NETS, no_connect=["U1.3", "U1.4", "U1.6", "U1.8"], libraries=LIBS, erc=False)
    rep = schematic_review.run_erc(out)
    assert rep and "error" not in rep, rep
    errors = [(v["type"], v["description"]) for s in rep.get("sheets", []) for v in s.get("violations", [])
              if v.get("severity") == "error"]
    assert not errors, errors

    net = tmp_path / "demo.net"
    p = run("sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(net), str(out))
    assert net.exists(), p.stderr or p.stdout
    got = {}
    for n in sexpr.parse(net.read_text(encoding="utf-8")).child("nets").children("net"):
        name = n.child("name").atoms()[0].lstrip("/")
        pins = {f"{x.child('ref').atoms()[0]}.{x.child('pin').atoms()[0]}" for x in n.children("node")}
        if len(pins) > 1 or not name.startswith("unconnected"):
            got[name] = pins
    want = {"+3V3": {"U1.1", "C1.1", "R1.1"}, "GND": {"U1.2", "C1.2", "R3.2"}, "RST": {"U1.7", "R1.2"},
            "LED": {"U1.5", "R2.1"}, "MID": {"R2.2", "R3.1"}}
    for name, pins in want.items():
        assert any(v == pins for v in got.values()), (name, got)
