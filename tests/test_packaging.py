"""Packaging: one version everywhere, and the skills reachable however cadmate is installed."""

import json
import os
from pathlib import Path

import pytest

import cadmate
from cadmate import cli

ROOT = Path(__file__).resolve().parents[1]


def test_versions_in_step():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == "cadmate")
    # Claude Code only offers a plugin update when this version changes, so a release must bump all three.
    assert plugin["version"] == entry["version"] == cadmate.__version__


def test_skills_found():
    names = {p.name for p in cli.skills_dir().iterdir() if (p / "SKILL.md").is_file()}
    assert "cadmate-project" in names and len(names) >= 10


def test_install_skills(tmp_path):
    dest = tmp_path / "skills"
    mine = dest / "protoboard"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("someone else's\n", encoding="utf-8")

    r = cli.install_skills(dest)
    assert r["skipped"] == ["protoboard"] and "cadmate-project" in r["installed"]
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "someone else's\n"
    assert (dest / "cadmate-project" / cli.MARKER).exists()

    # a reinstall replaces cadmate's own folders, still not the user's
    (dest / "cadmate-project" / "stale.md").write_text("old\n", encoding="utf-8")
    r = cli.install_skills(dest)
    assert r["skipped"] == ["protoboard"] and not (dest / "cadmate-project" / "stale.md").exists()

    assert cli.install_skills(dest, force=True)["skipped"] == []
    assert (mine / cli.MARKER).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need developer mode on Windows")
def test_install_skills_replaces_old_symlinks(tmp_path):
    dest = tmp_path / "skills"
    dest.mkdir()
    (dest / "kicad-parts").symlink_to(cli.skills_dir() / "kicad-parts", target_is_directory=True)
    cli.install_skills(dest)
    assert not (dest / "kicad-parts").is_symlink() and (dest / "kicad-parts" / "SKILL.md").is_file()
    assert (cli.skills_dir() / "kicad-parts" / "SKILL.md").is_file()  # the source wasn't touched


def test_version_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert cadmate.__version__ in capsys.readouterr().out
