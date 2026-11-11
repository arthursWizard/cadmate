"""Find KiCad library symbols by their library nickname ("Device:R", "power:GND"), as KiCad would.

A nickname is looked up, first match wins:

1. the libraries passed in (nickname -> .kicad_sym file or .kicad_symdir folder);
2. the project's sym-lib-table, beside the schematic;
3. KiCad's own sym-lib-table in the user's KiCad settings;
4. KiCad's installed symbol folder (``KICAD10_SYMBOL_DIR``, or next to kicad-cli), as ``<nickname>.kicad_symdir`` or
   ``<nickname>.kicad_sym``.

A symbol comes back as the text a schematic embeds in its lib_symbols (named "<nickname>:<name>", a derived symbol
flattened onto its parent's drawing and pins) plus the parsed Symbol for its pins and outline.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

from . import apps, sexpr, symbol

KICAD_MAJOR = "10"
SYMBOL_DIR_ENV = f"KICAD{KICAD_MAJOR}_SYMBOL_DIR"
FOOTPRINT_DIR_ENV = f"KICAD{KICAD_MAJOR}_FOOTPRINT_DIR"


class LibraryError(ValueError):
    pass


def _expand(uri: str, project: Path | None) -> Path:
    def var(m: re.Match) -> str:
        name = m[1]
        if name == "KIPRJMOD" and project is not None:
            return str(project)
        if name == SYMBOL_DIR_ENV and name not in os.environ:
            d = installed_symbol_dir()
            return str(d) if d else m[0]
        if name == FOOTPRINT_DIR_ENV and name not in os.environ:
            d = installed_footprint_dir()
            return str(d) if d else m[0]
        return os.environ.get(name, m[0])
    return Path(re.sub(r"\$\{(\w+)\}", var, uri)).expanduser()


def _table(path: Path, project: Path | None) -> dict[str, Path]:
    if not path.is_file():
        return {}
    try:
        root = sexpr.parse(path.read_text(encoding="utf-8", errors="replace"))
    except sexpr.SexprError:
        return {}
    out = {}
    for lib in root.children("lib"):
        name, uri = lib.child("name"), lib.child("uri")
        if name is not None and uri is not None and name.atoms() and uri.atoms():
            out.setdefault(name.atoms()[0], _expand(uri.atoms()[0], project))
    return out


def _installed_dir(env: str, kind: str) -> Path | None:
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    cli = apps.find_kicad_cli()
    guesses = []
    if cli:
        c = Path(cli).resolve()
        guesses += [c.parent.parent / "share" / "kicad" / kind,                     # Linux, Windows
                    c.parent.parent / "SharedSupport" / kind]                       # macOS app bundle
    guesses += [Path(f"/usr/share/kicad/{kind}"), Path(f"/usr/local/share/kicad/{kind}"),
                Path(f"/Applications/KiCad/KiCad.app/Contents/SharedSupport/{kind}"),
                Path(f"C:/Program Files/KiCad/{KICAD_MAJOR}.0/share/kicad/{kind}")]
    return next((g for g in guesses if g.is_dir()), None)


def installed_symbol_dir() -> Path | None:
    """KiCad's own symbol libraries: KICAD10_SYMBOL_DIR, else the share folder that goes with kicad-cli."""
    return _installed_dir(SYMBOL_DIR_ENV, "symbols")


def installed_footprint_dir() -> Path | None:
    """KiCad's own footprint libraries: KICAD10_FOOTPRINT_DIR, else the share folder that goes with kicad-cli."""
    return _installed_dir(FOOTPRINT_DIR_ENV, "footprints")


def _user_table(name: str = "sym-lib-table") -> Path:
    system = platform.system()
    if system == "Darwin":
        base = Path("~/Library/Preferences/kicad").expanduser()
    elif system == "Windows":
        base = Path(os.environ.get("APPDATA", "~")).expanduser() / "kicad"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "kicad"
    return base / f"{KICAD_MAJOR}.0" / name


class Libraries:
    """Resolves nicknames for one schematic, caching what it has read."""

    def __init__(self, near: Path | None = None, libraries: dict[str, str] | None = None):
        self.project = near if near is None or near.is_dir() else near.parent
        self.given = {k: Path(v).expanduser() for k, v in (libraries or {}).items()}
        self._tables: list[dict[str, Path]] | None = None
        self._cache: dict[str, tuple[str, symbol.Symbol]] = {}

    def path(self, nickname: str) -> Path | None:
        if nickname in self.given:
            return self.given[nickname]
        if self._tables is None:
            self._tables = [_table(self.project / "sym-lib-table", self.project) if self.project else {},
                            _table(_user_table(), self.project)]
        for t in self._tables:
            if nickname in t:
                return t[nickname]
        d = installed_symbol_dir()
        if d:
            for cand in (d / f"{nickname}.kicad_symdir", d / f"{nickname}.kicad_sym"):
                if cand.exists():
                    return cand
        return None

    def get(self, lib_id: str) -> tuple[str, symbol.Symbol]:
        """(text to embed in lib_symbols, parsed symbol) for "nickname:name"."""
        if lib_id in self._cache:
            return self._cache[lib_id]
        if ":" not in lib_id:
            raise LibraryError(f"{lib_id!r}: give the symbol as library:name, e.g. Device:R")
        nick, name = lib_id.split(":", 1)
        where = self.path(nick)
        if where is None or not where.exists():
            raise LibraryError(f"library {nick!r} not found: not in the project's or KiCad's sym-lib-table, nor in "
                               f"KiCad's symbol folder (set {SYMBOL_DIR_ENV}); pass libraries {{\"{nick}\": path}}")
        nodes = _nodes(where, name)
        if name not in nodes:
            raise LibraryError(f"no symbol {name!r} in library {nick!r} ({where})")
        text = _flatten(nodes, name, lib_id)
        parsed = symbol.parse_symbol(text.replace(f'(symbol {sexpr.quote(lib_id)}', f'(symbol {sexpr.quote(name)}', 1))
        self._cache[lib_id] = (text, parsed)
        return text, parsed


def _nodes(where: Path, name: str) -> dict[str, tuple[str, sexpr.Node]]:
    """Symbol name -> (its file's text, its node): the one asked for and its parents."""
    out: dict[str, tuple[str, sexpr.Node]] = {}
    want = [name]
    loaded_whole = False
    while want:
        n = want.pop()
        if n in out:
            continue
        files = [where / f"{n}.kicad_sym"] if where.is_dir() else [where]
        if not where.is_dir() and loaded_whole:
            files = []
        for f in files:
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            root = sexpr.parse(text)
            for node in root.children("symbol"):
                out.setdefault(node.atoms()[0], (text, node))
            loaded_whole = True
        if n in out:
            ext = out[n][1].child("extends")
            if ext is not None and ext.atoms():
                want.append(ext.atoms()[0])
    return out


def _flatten(nodes: dict[str, tuple[str, sexpr.Node]], name: str, lib_id: str) -> str:
    """The symbol as a schematic embeds it: renamed to lib_id, with a parent's drawing and pins if it extends one."""
    text, node = nodes[name]
    own = [i for i in node.items[2:] if isinstance(i, sexpr.Node)]
    parent_name = node.child("extends").atoms()[0] if node.child("extends") is not None else None
    if parent_name is None:
        body = text[node.start:node.end]
        return body.replace(f"(symbol {sexpr.quote(name)}", f"(symbol {sexpr.quote(lib_id)}", 1)
    # walk up to the root parent, which holds the drawing
    chain = [name]
    p = parent_name
    while p and p in nodes and p not in chain:
        chain.append(p)
        ext = nodes[p][1].child("extends")
        p = ext.atoms()[0] if ext is not None and ext.atoms() else None
    root_name = chain[-1]
    ptext, pnode = nodes[root_name]
    props: dict[str, str] = {}
    for sym in reversed(chain):  # parent first, then each child's fields override
        t, n = nodes[sym]
        for prop in n.children("property"):
            props[prop.atoms()[0]] = t[prop.start:prop.end]
    parts = [f"(symbol {sexpr.quote(lib_id)}"]
    for item in pnode.items[2:]:
        if not isinstance(item, sexpr.Node) or item.head in ("property", "extends"):
            continue
        piece = ptext[item.start:item.end]
        if item.head == "symbol" and item.atoms():
            sub = item.atoms()[0]
            piece = piece.replace(f"(symbol {sexpr.quote(sub)}", f"(symbol {sexpr.quote(name + sub[len(root_name):])}", 1)
        parts.append(piece)
    # the child's own flags (in_bom, power...) win over the parent's
    for item in own:
        if item.head not in ("property", "extends", "symbol"):
            piece = text[item.start:item.end]
            parts = [p for p in parts if not p.startswith(f"({item.head} ") and not p.startswith(f"({item.head})")] + [piece]
    parts[1:1] = list(props.values())
    return "\n".join(parts) + "\n)"


class Footprints:
    """Footprint libraries by nickname ("Resistor_SMD:R_0603_1608Metric"), found as KiCad finds them: the libraries
    passed in (nickname -> .pretty folder), the project's fp-lib-table, KiCad's own, then its installed footprints."""

    def __init__(self, near: Path | None = None, libraries: dict[str, str] | None = None):
        self.project = near if near is None or near.is_dir() else near.parent
        self.given = {k: Path(v).expanduser() for k, v in (libraries or {}).items()}
        self._tables: list[dict[str, Path]] | None = None

    def path(self, nickname: str) -> Path | None:
        if nickname in self.given:
            return self.given[nickname]
        if self._tables is None:
            self._tables = [_table(self.project / "fp-lib-table", self.project) if self.project else {},
                            _table(_user_table("fp-lib-table"), self.project)]
        for t in self._tables:
            if nickname in t:
                return t[nickname]
        d = installed_footprint_dir()
        if d and (d / f"{nickname}.pretty").is_dir():
            return d / f"{nickname}.pretty"
        return None

    def get(self, lib_id: str) -> Path:
        """The .kicad_mod file for "nickname:name"."""
        if ":" not in lib_id:
            raise LibraryError(f"{lib_id!r}: give the footprint as library:name, e.g. Resistor_SMD:R_0603_1608Metric")
        nick, name = lib_id.split(":", 1)
        where = self.path(nick)
        if where is None or not where.is_dir():
            raise LibraryError(f"footprint library {nick!r} not found: not in the project's or KiCad's fp-lib-table, "
                               f"nor in KiCad's footprint folder (set {FOOTPRINT_DIR_ENV}); pass libraries "
                               f"{{\"{nick}\": path to the .pretty folder}}")
        f = where / f"{name}.kicad_mod"
        if not f.is_file():
            raise LibraryError(f"no footprint {name!r} in library {nick!r} ({where})")
        return f
