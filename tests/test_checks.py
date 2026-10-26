from pathlib import Path

from cadmate import check, footprint, packages

FIX = Path(__file__).parent / "fixtures"
SOIC = footprint.read(FIX / "SOIC-8.kicad_mod")
CAP = footprint.read(FIX / "CP_Radial_D5.0mm_P2.00mm.kicad_mod")

SOIC_PARAMS = {
    "pins": 8, "pitch": 1.27, "body_length": 4.9, "body_width": 3.9, "overall_width": 6.0,
    "height": 1.75, "standoff": 0.1, "lead_width": 0.41, "lead_length": 0.8,
}


def test_soic_params_match_footprint():
    r = packages.check_params("soic", SOIC_PARAMS, SOIC)
    assert r["ok"], r


def test_wrong_pitch_and_missing_values_are_reported():
    r = packages.check_params("soic", SOIC_PARAMS | {"pitch": 0.65}, SOIC)
    assert not r["ok"]
    assert any("pitch" in e for e in r["errors"])
    r = packages.check_params("soic", {"pins": 8})
    assert "missing pitch" in r["errors"]


def test_impossible_geometry():
    r = packages.check_params("soic", SOIC_PARAMS | {"standoff": 2.0})
    assert any("standoff" in e for e in r["errors"])


def test_radial_cap_against_old_footprint():
    r = packages.check_params("radial_cap", {"diameter": 5, "height": 11, "lead_spacing": 2, "lead_diameter": 0.5}, CAP)
    assert r["ok"], r


# SOIC-8 model: body plus leads span 6.0 x 4.9 mm, centred, sitting on the board.
GOOD = {"xmin": -3.0, "xmax": 3.0, "ymin": -2.45, "ymax": 2.45, "zmin": 0.0, "zmax": 1.75}


def test_good_model_passes():
    r = check.check_model(SOIC, GOOD, expected={"length": 6.0, "width": 4.9, "height": 1.75})
    assert r["ok"], r
    assert r["warnings"] == []


def test_shifted_model_fails():
    shifted = GOOD | {"xmin": -2.0, "xmax": 4.0}
    r = check.check_model(SOIC, shifted)
    assert not r["ok"]
    assert "centre" in r["errors"][0]


def test_rotation_swaps_axes():
    rotated = {"xmin": -2.45, "xmax": 2.45, "ymin": -3.0, "ymax": 3.0, "zmin": 0.0, "zmax": 1.75}
    r = check.check_model(SOIC, rotated, footprint.Model(path="", rotate=(0, 0, 90)), {"length": 6.0, "width": 4.9})
    assert r["ok"], r


def test_too_tall_and_sunk_model():
    r = check.check_model(SOIC, GOOD | {"zmin": -0.3, "zmax": 2.5}, expected={"height": 1.75})
    assert len(r["errors"]) == 2


def test_y_up_model_is_stood_up_by_x_rotation():
    # A Y-up model (height along +Y), as many vendor STEPs are; KiCad's rotate X = -90 stands it on the board.
    y_up = {"xmin": -3.0, "xmax": 3.0, "ymin": 0.0, "ymax": 1.75, "zmin": -2.45, "zmax": 2.45}
    r = check.check_model(SOIC, y_up, footprint.Model(path="", rotate=(-90, 0, 0)), {"length": 6.0, "width": 4.9, "height": 1.75})
    assert r["ok"], r
    assert r["measured"]["model_bbox_placed"]["zmin"] == 0.0


def test_z_rotation_direction():
    # KiCad's rotate Z = 90 turns the model clockwise seen from above: +X goes to -Y.
    off_centre = {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 0.0, "zmin": 0.0, "zmax": 1.0}
    b = check._apply_placement(off_centre, footprint.Model(path="", rotate=(0, 0, 90)))
    assert (b["ymin"], b["ymax"], b["xmin"], b["xmax"]) == (-1.0, 0.0, 0.0, 0.0)


def test_odd_angles_are_refused():
    r = check.check_model(SOIC, GOOD, footprint.Model(path="", rotate=(0, 0, 45)))
    assert not r["ok"]
