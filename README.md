# cadmate

An AI companion for KiCad and FreeCAD: your CAD mate. It carries work across the two apps, footprints and boards from KiCad into FreeCAD and 3D models and enclosures back, and brings EE, PCB and 3D-printing know-how along.

cadmate is an [MCP](https://modelcontextprotocol.io) server. You talk to Claude (Claude Code or Claude Desktop). Claude uses cadmate for the KiCad side and the cross-app checks, and uses an existing FreeCAD MCP server for the modelling.

## Status

Early. Three workflows work end to end:

- **Make the missing 3D model**: point Claude at a footprint and the part's datasheet, and get a checked STEP model linked into that footprint.
- **Make a footprint from a datasheet**: Claude reads the package drawing, cadmate computes IPC-7351B pads (or places the datasheet's recommended land pattern) and writes a checked `.kicad_mod`. For JEDEC packages the pads and courtyard match KiCad's own library footprints exactly.
- **Make a symbol from a datasheet**: Claude reads the pin table, cadmate lays out the symbol per KiCad's library conventions (grid, sides by pin type, stacked power pins, hidden NC pins, fields) and writes it into a `.kicad_sym` library, checked against the pin table and the footprint's pads.

| Tool | What it does | Needs |
| --- | --- | --- |
| `environment` | Finds KiCad and FreeCAD and reports versions and available tools | nothing |
| `read_footprint` | Pads, pitch, pin 1, courtyard and linked models from a `.kicad_mod` | nothing |
| `list_package_families` | Package families and the datasheet dimensions each needs | nothing |
| `package_params` | Checks datasheet dimensions, and cross-checks them with the footprint | nothing |
| `link_model_to_footprint` | Writes the 3D model link into the footprint, keeping a `.bak` | nothing |
| `check_model_vs_footprint` | Checks a STEP model's centring, seating height, courtyard and size | FreeCAD |
| `list_footprint_families` | Footprint families and the datasheet values each needs | nothing |
| `generate_footprint` | Writes a footprint from datasheet dimensions into a `.pretty` library, checked first | nothing |
| `check_footprint` | Reviews any footprint: touching pads, courtyard, silkscreen on copper, KiCad library rules; optionally against the datasheet | nothing |
| `compare_footprints` | Pad-by-pad differences between two footprints | nothing |
| `generate_symbol` | Writes a symbol from the datasheet's pin table into a `.kicad_sym` library, checked first | nothing |
| `check_symbol` | Reviews any symbol against KiCad's library rules (KLC); optionally against the pin table and the footprint's pads | nothing |
| `read_symbol` | Pins, sides, units and fields from a `.kicad_sym` file or `.kicad_symdir` folder | nothing |
| `compare_symbols` | Pin-by-pin differences between two symbols | nothing |

## Version policy

cadmate requires **KiCad 10** and **FreeCAD 1.1** or later, and follows new releases rather than old ones: it writes the file formats of these versions, which later versions keep reading. `environment` reports an older install as unsupported.

- **KiCad files** are read directly: footprints from KiCad 5 (`module`) to current (`footprint`) formats, symbol libraries from KiCad 6 on, including KiCad 10's one-file-per-symbol `.kicad_symdir` folders. They are written in KiCad 10's format, as KiCad's own editors write it. No KiCad install is needed for footprint or symbol work.
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
- `kicad-footprints`: how to read a datasheet into a footprint, and review an existing one.
- `kicad-symbols`: how to read a datasheet's pin table into a symbol, and review an existing one.

## Roadmap

1. Missing 3D model for a footprint (done)
2. Footprints from datasheets (gull-wing, QFN/DFN, custom land patterns, through-hole holes and slots sized from the leads; SOT, chip parts and DIP/header families next)
3. Symbols from datasheets (rectangular and opamp bodies, multi-unit, alternate pin functions; logic gate shapes next)
4. Board review: ERC/DRC with explanations and fixes
5. Fab export per fab house
6. Enclosure from the board
7. Live PCB help through the IPC API
8. Schematic help (live once KiCad 11's schematic API ships)
9. Buttons inside KiCad and FreeCAD that hand the current selection to Claude

## License

MIT. Tools under other licenses (for example AGPL tools such as KiBot) are only ever called as separate programs, never imported.
