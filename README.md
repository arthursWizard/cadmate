# cadmate

An AI companion for KiCad and FreeCAD: your CAD mate. It carries work across the two apps, footprints and boards from KiCad into FreeCAD and 3D models and enclosures back, and brings EE, PCB and 3D-printing know-how along.

cadmate is an [MCP](https://modelcontextprotocol.io) server. You talk to Claude (Claude Code or Claude Desktop). Claude uses cadmate for the KiCad side and the cross-app checks, and uses an existing FreeCAD MCP server for the modelling.

## Status

Early. Milestone 1 is **"make the missing 3D model"**: point Claude at a footprint and the part's datasheet, and get a checked STEP model linked into that footprint.

| Tool | What it does | Needs |
| --- | --- | --- |
| `environment` | Finds KiCad and FreeCAD and reports versions and available tools | nothing |
| `read_footprint` | Pads, pitch, pin 1, courtyard and linked models from a `.kicad_mod` | nothing |
| `list_package_families` | Package families and the datasheet dimensions each needs | nothing |
| `package_params` | Checks datasheet dimensions, and cross-checks them with the footprint | nothing |
| `link_model_to_footprint` | Writes the 3D model link into the footprint, keeping a `.bak` | nothing |
| `check_model_vs_footprint` | Checks a STEP model's centring, seating height, courtyard and size | FreeCAD |

## Version policy

cadmate uses the most stable interface that does the job, so it is not tied to one release:

- **KiCad files** are read directly, from KiCad 5 (`module`) to current (`footprint`) formats. No KiCad install is needed for footprint work.
- **`kicad-cli`** (KiCad 7+) for exports and checks, in later milestones.
- **KiCad IPC API** (KiCad 9+) only for live editing, switched on when present.
- **FreeCAD** through `FreeCADCmd`, using the Part API that is stable across 0.20 to 1.1.

Developed against KiCad 10.0 and FreeCAD 1.1 on macOS.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> && cd cadmate
uv sync
uv run pytest
```

Add it to Claude Code:

```sh
claude mcp add cadmate -- uv run --directory /path/to/cadmate cadmate
```

For modelling, also add a FreeCAD MCP server. The recommended one is [spkane/freecad-addon-robust-mcp-server](https://github.com/spkane/freecad-addon-robust-mcp-server) (MIT); follow its README. On macOS, run it against the FreeCAD app (socket/XML-RPC mode), not its embedded mode.

cadmate finds KiCad and FreeCAD in their usual install locations on macOS, Linux and Windows. To point it elsewhere, set these in the server's environment:

| Variable | Example |
| --- | --- |
| `CADMATE_KICAD_CLI` | `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` |
| `CADMATE_FREECADCMD` | `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd` |

KiCad path variables in model paths (for example `${KICAD10_3DMODEL_DIR}`) are expanded from the same environment. `${KIPRJMOD}` falls back to the footprint's folder.

## Skills

`skills/` holds the domain knowledge Claude loads for each workflow. Copy or symlink a skill folder into `~/.claude/skills/` (or your project's `.claude/skills/`) to use it.

- `kicad-3d-models`: how to build, check and link a 3D model for a KiCad footprint.

## Roadmap

1. Missing 3D model for a footprint (in progress)
2. Board review: ERC/DRC with explanations and fixes
3. Fab export per fab house
4. Enclosure from the board
5. Live PCB help through the IPC API
6. Symbols and footprints from datasheets
7. Schematic help (live once KiCad 11's schematic API ships)
8. Buttons inside KiCad and FreeCAD that hand the current selection to Claude

## License

MIT. Tools under other licenses (for example AGPL tools such as KiBot) are only ever called as separate programs, never imported.
