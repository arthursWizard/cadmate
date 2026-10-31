"""Finding KiCad and FreeCAD on each platform, and what cadmate ties to their versions. No install needed: the
install folders are faked under tmp_path and the platform is patched."""

from pathlib import Path

import pytest

from cadmate import apps, freerouting, versions


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


@pytest.fixture
def no_path(monkeypatch):
    """Nothing on the PATH and no overrides, so only the install locations count."""
    monkeypatch.setattr(apps.shutil, "which", lambda name: None)
    monkeypatch.setattr(freerouting.shutil, "which", lambda name: None)
    for var in (apps.KICAD_CLI_ENV, apps.FREECADCMD_ENV, freerouting.JAVA_ENV, freerouting.KICAD_PYTHON_ENV, "JAVA_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_windows_picks_the_newest_kicad_by_number_not_by_text(tmp_path, monkeypatch, no_path):
    # As text "9.0" sorts after "10.0"; the user with both installed wants 10.
    for v in ("9.0", "10.0"):
        touch(tmp_path / "Program Files" / "KiCad" / v / "bin" / "kicad-cli.exe")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    cands = {"Windows": ["${ProgramFiles}/KiCad/*/bin/kicad-cli.exe"]}  # / so the test runs on any OS
    assert Path(apps.locate(apps.KICAD_CLI_ENV, ["kicad-cli"], cands, system="Windows")).parts[-3] == "10.0"


def test_windows_per_user_install_is_found(tmp_path, monkeypatch, no_path):
    exe = touch(tmp_path / "Local" / "Programs" / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nothing here"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    cands = {"Windows": [p.replace("\\", "/") for p in apps._KICAD_CANDIDATES["Windows"]]}
    assert apps.locate(apps.KICAD_CLI_ENV, ["kicad-cli"], cands, system="Windows") == str(exe)


def test_every_platform_has_install_locations():
    for table in (apps._KICAD_CANDIDATES, apps._FREECAD_CANDIDATES):
        assert set(table) == {"Darwin", "Linux", "Windows"} and all(table.values())
    # Flatpak's export is KiCad's launcher, not kicad-cli: running "version" on it would open KiCad
    assert not any("flatpak" in p for p in apps._KICAD_CANDIDATES["Linux"])


def test_override_wins_and_a_wrong_one_is_explained(tmp_path, monkeypatch):
    cli = touch(tmp_path / "my kicad" / "kicad-cli")
    monkeypatch.setenv(apps.KICAD_CLI_ENV, str(cli))
    assert apps.find_kicad_cli() == str(cli)
    monkeypatch.setenv(apps.KICAD_CLI_ENV, str(tmp_path / "gone"))
    assert apps.find_kicad_cli() is None
    info = apps.kicad_info()
    assert not info.available and "doesn't exist" in info.note and apps.KICAD_CLI_ENV in info.note


def test_kicad_python_prefers_the_newest_python(tmp_path, monkeypatch, no_path):
    for v in ("3.9", "3.11"):
        touch(tmp_path / "Versions" / v / "bin" / "python3")
    monkeypatch.setattr(freerouting, "_KICAD_PYTHONS", {"Darwin": [str(tmp_path / "Versions" / "*" / "bin" / "python3")]})
    monkeypatch.setattr(freerouting.platform, "system", lambda: "Darwin")
    tried = []

    class Done:
        returncode = 0

    monkeypatch.setattr(freerouting.subprocess, "run", lambda cmd, **kw: tried.append(cmd[0]) or Done())
    assert Path(freerouting.find_kicad_python()).parts[-3] == "3.11" and len(tried) == 1


def test_java_home_on_windows_has_an_exe(tmp_path, monkeypatch, no_path):
    java = touch(tmp_path / "jdk" / "bin" / "java.exe")
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "jdk"))
    assert freerouting.find_java() == str(java)


@pytest.mark.parametrize("app,version,status", [
    ("kicad", "10.0.4", "supported"),
    ("kicad", "9.0.2", "too_old"),
    ("kicad", "10.99.0-1234-g5a6b", "newer_than_tested"),
    ("kicad", "11.0.0", "newer_than_tested"),
    ("freecad", "1.1.0", "supported"),
    ("freecad", "0.21.2", "too_old"),
    ("freecad", None, "unknown"),
])
def test_version_status(app, version, status):
    assert versions.status(app, version) == status


def test_newer_install_is_supported_but_flagged():
    d = apps.AppInfo("kicad", "/x/kicad-cli", "11.0.0").to_dict()
    assert d["supported"] is True and d["status"] == "newer_than_tested" and "versions.py" in d["note"]
    d = apps.AppInfo("kicad", "/x/kicad-cli", "9.0.0").to_dict()
    assert d["supported"] is False and "older" in d["note"]


def test_written_formats_come_from_versions():
    from cadmate import generate, part, symbol

    assert generate.FORMAT_VERSION == versions.FOOTPRINT_FORMAT
    assert symbol.FORMAT_VERSION == versions.SYMBOL_FORMAT
    assert part.LIB_TABLE_VERSION == versions.LIB_TABLE_FORMAT


def test_kicad_cli_args_are_a_fresh_list():
    a = versions.kicad_cli_args("pcb_drc")
    a.append("--extra")
    assert "--extra" not in versions.kicad_cli_args("pcb_drc") and a[:2] == ["pcb", "drc"]
