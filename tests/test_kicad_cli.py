"""Run the kicad-cli commands cadmate uses against the real kicad-cli, so a release that renames a flag or changes the
report shape fails here instead of in front of a user. Skipped when kicad-cli isn't installed; CI runs them on Linux
with KiCad installed (see .github/workflows/tests.yml)."""

import os
import subprocess
from pathlib import Path

import pytest

from cadmate import apps, pcb_review, versions
from test_pcb_review import HEAD, footprint, pad

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
