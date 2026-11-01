"""The `cadmate` command.

With no arguments it runs the MCP server on stdio, which is what Claude starts. The other commands are for people:

    cadmate --version
    cadmate install-skills [--dest DIR] [--force]   copy cadmate's skills where Claude Code reads them
    cadmate skills-path                             print where the bundled skills are

The skills ship inside the package (cadmate/skills in the wheel). In a source checkout they are at the repo root.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__

MARKER = ".cadmate-skill"  # written into each installed skill, so a reinstall only replaces cadmate's own folders


def skills_dir() -> Path:
    """The skills folder: inside the installed package, or at the root of a source checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "skills", here.parents[1] / "skills"):
        if (candidate / "cadmate-project" / "SKILL.md").is_file():
            return candidate
    raise FileNotFoundError("cadmate's skills folder is missing from this install")


def install_skills(dest: Path, force: bool = False) -> dict:
    """Copy each bundled skill into dest/<name>/. Replaces what a previous install (or the old symlink setup) left;
    leaves any other folder of the same name alone unless force is set."""
    src = skills_dir()
    dest.mkdir(parents=True, exist_ok=True)
    done, skipped = [], []
    for skill in sorted(p for p in src.iterdir() if (p / "SKILL.md").is_file()):
        target = dest / skill.name
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            if not (force or (target / MARKER).exists()):
                skipped.append(skill.name)
                continue
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        shutil.copytree(skill, target)
        (target / MARKER).write_text(f"installed by cadmate {__version__}; `cadmate install-skills` replaces this folder\n",
                                     encoding="utf-8")
        done.append(skill.name)
    return {"dest": str(dest), "installed": done, "skipped": skipped}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cadmate", description="cadmate MCP server for KiCad and FreeCAD. "
                                     "With no command it serves MCP on stdio, which is how Claude starts it.")
    parser.add_argument("--version", action="version", version=f"cadmate {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (the default)")
    inst = sub.add_parser("install-skills", help="copy cadmate's skills where Claude Code reads them")
    inst.add_argument("--dest", type=Path, default=Path.home() / ".claude" / "skills",
                      help="skills folder (default: ~/.claude/skills; use <project>/.claude/skills for one project)")
    inst.add_argument("--force", action="store_true", help="also replace same-named folders cadmate didn't install")
    sub.add_parser("skills-path", help="print where the bundled skills are")
    args = parser.parse_args(argv)

    if args.command == "install-skills":
        r = install_skills(args.dest.expanduser(), force=args.force)
        print(f"cadmate {__version__}: installed {len(r['installed'])} skills into {r['dest']}")
        if r["skipped"]:
            print(f"skipped (a folder of that name exists that cadmate didn't install; --force replaces it): "
                  f"{', '.join(r['skipped'])}", file=sys.stderr)
        print("Start a new Claude Code session to pick them up.")
    elif args.command == "skills-path":
        print(skills_dir())
    else:
        from .server import main as serve  # imported here so --version and the skills commands stay fast
        serve()


if __name__ == "__main__":
    main()
