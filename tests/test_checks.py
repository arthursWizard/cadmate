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
    r = check.check_model(SOIC, GOOD, expected={"width": 6.0, "length": 4.9, "height": 1.75})
    assert r["ok"], r
    assert r["warnings"] == []


def test_shifted_model_fails():
    shifted = GOOD | {"xmin": -2.0, "xmax": 4.0}
    r = check.check_model(SOIC, shifted)
    assert not r["ok"]
    assert "centre" in r["errors"][0]


def test_rotation_swaps_axes():
    rotated = {"xmin": -2.45, "xmax": 2.45, "ymin": -3.0, "ymax": 3.0, "zmin": 0.0, "zmax": 1.75}
    r = check.check_model(SOIC, rotated, footprint.Model(path="", rotate=(0, 0, 90)), {"width": 6.0, "length": 4.9})
    assert r["ok"], r


def test_too_tall_and_sunk_model():
    r = check.check_model(SOIC, GOOD | {"zmin": -0.3, "zmax": 2.5}, expected={"height": 1.75})
    assert len(r["errors"]) == 2


def test_y_up_model_is_stood_up_by_x_rotation():
    # A Y-up model (height along +Y), as many vendor STEPs are; KiCad's rotate X = -90 stands it on the board.
    y_up = {"xmin": -3.0, "xmax": 3.0, "ymin": 0.0, "ymax": 1.75, "zmin": -2.45, "zmax": 2.45}
    r = check.check_model(SOIC, y_up, footprint.Model(path="", rotate=(-90, 0, 0)), {"width": 6.0, "length": 4.9, "height": 1.75})
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


RP2040_ZERO = footprint.read(Path(__file__).parent.parent / "examples" / "rp2040-zero" / "RP2040-Zero.kicad_mod")
# Generated model: board on the courtyard, USB-C 1 mm past the top edge, parts under the board.
RP2040_MODEL = {"xmin": 1.16, "xmax": 19.16, "ymin": 0.95, "ymax": 25.45, "zmin": -1.1, "zmax": 4.76}


def test_overhang_is_a_warning_not_silent():
    r = check.check_model(RP2040_ZERO, RP2040_MODEL, expected={"width": 18, "length": 23.5})
    assert r["ok"], r
    assert any("length" in w and "over the datasheet" in w for w in r["warnings"])


def test_through_hole_model_off_the_pads_is_caught():
    shifted = RP2040_MODEL | {"xmin": 4.16, "xmax": 22.16}
    r = check.check_model(RP2040_ZERO, shifted)
    assert any("holes lie" in w for w in r["warnings"])
    far = RP2040_MODEL | {"xmin": 31.16, "xmax": 49.16}
    assert not check.check_model(RP2040_ZERO, far)["ok"]


# The user's other RP2040-Zero STEP: Y-up, centred, linked with offset (10.25, 12.75, 0) and rotate (-90, 0, 0).
# It shows the right way up in KiCad, which fixes the sign: a file value of -90 about X turns +Y into +Z.
Y_UP_ZERO = {"xmin": -9.0, "xmax": 9.0, "ymin": -1.0, "ymax": 4.35, "zmin": -12.95, "zmax": 11.75}


def test_kicad_rotation_sign_matches_a_real_footprint():
    m = footprint.Model(path="", offset=(10.25, 12.75, 0), rotate=(-90, 0, 0))
    b = check._apply_placement(Y_UP_ZERO, m)
    assert (round(b["zmin"], 2), round(b["zmax"], 2)) == (-1.0, 4.35)
    assert not any("upside down" in w for w in check.check_model(RP2040_ZERO, Y_UP_ZERO, m)["warnings"])


def test_upside_down_module_is_flagged():
    m = footprint.Model(path="", offset=(10.25, 12.75, 0), rotate=(90, 0, 0))
    r = check.check_model(RP2040_ZERO, Y_UP_ZERO, m)
    assert any("upside down" in w for w in r["warnings"])


def test_older_apps_are_reported_unsupported():
    from cadmate import apps

    assert apps.AppInfo("kicad", "/x", "10.0.4").supported
    assert apps.AppInfo("kicad", "/x", "11.0.0").supported
    old = apps.AppInfo("kicad", "/x", "9.0.3").to_dict()
    assert old["supported"] is False and "older than 10.0" in old["note"]
    assert apps.AppInfo("freecad", "/x", "1.0.2").supported is False
    assert apps.AppInfo("freecad", "/x", None).supported is None


def test_swapped_width_and_length_are_named():
    r = check.check_model(SOIC, GOOD, expected={"width": 4.9, "length": 6.0})
    assert not r["ok"]
    assert any("width is along X" in e for e in r["errors"])
