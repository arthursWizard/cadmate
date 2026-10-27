# cadmate

An AI companion for KiCad and FreeCAD: your CAD mate. It carries work across the two apps, footprints and boards from KiCad into FreeCAD and 3D models and enclosures back, and brings EE, PCB and 3D-printing know-how along.

cadmate is an [MCP](https://modelcontextprotocol.io) server. You talk to Claude (Claude Code or Claude Desktop). Claude uses cadmate for the KiCad side and the cross-app checks, and uses an existing FreeCAD MCP server for the modelling.

## Status

Early. Four workflows work end to end:

- **Make the missing 3D model**: point Claude at a footprint and the part's datasheet, and get a checked STEP model linked into that footprint.
- **Make a footprint from a datasheet**: Claude reads the package drawing, cadmate computes IPC-7351B pads (or places the datasheet's recommended land pattern) and writes a checked `.kicad_mod`. For JEDEC packages the pads and courtyard match KiCad's own library footprints exactly.
- **Make a whole part from one datasheet**: symbol, footprint and 3D model from one reading of the datasheet, linked to each other (the symbol's Footprint field, the footprint's model link), checked together and registered in the project's library tables.
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
| `review_footprint` | Reviews any footprint against its 3D model, the datasheet and the symbol: swapped pad numbers, parts under a module that would hit the board, bodies outside the courtyard, castellated pads short of the module edge, holes too small for the lead | FreeCAD for the model |
| `compare_footprints` | Pad-by-pad differences between two footprints | nothing |
| `generate_symbol` | Writes a symbol from the datasheet's pin table into a `.kicad_sym` library, checked first | nothing |
| `check_symbol` | Reviews any symbol against KiCad's library rules (KLC); optionally against the pin table and the footprint's pads | nothing |
| `read_symbol` | Pins, sides, units and fields from a `.kicad_sym` file or `.kicad_symdir` folder | nothing |
| `compare_symbols` | Pin-by-pin differences between two symbols | nothing |
| `generate_part` | Writes a linked symbol, footprint and 3D model from one datasheet into a project, checked together first | FreeCAD for the model |
| `check_part` | Follows a symbol to its footprint and model and checks all three and the links between them | FreeCAD for the model check |

## Version policy

cadmate requires **KiCad 10** and **FreeCAD 1.1** or later, and follows new releases rather than old ones: it writes the file formats of these versions, which later versions keep reading. `environment` reports an older install as unsupported.

- **KiCad files** are read directly: footprints from KiCad 5 (`module`) to current (`footprint`) formats, symbol libraries from KiCad 6 on, including KiCad 10's one-file-per-symbol `.kicad_symdir` folders. They are written in KiCad 10's format, as KiCad's own editors write it. No KiCad install is needed for footprint or symbol work.
- **`kicad-cli`** (KiCad 7+) for exports and checks, in later milestones.
- **KiCad IPC API** (KiCad 9+) only for live editing, switched on when present.
- **FreeCAD** through `FreeCADCmd`, using the Part API that is stable across 0.20 to 1.1. Models it builds headless have no colours.

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
claude mcp add --scope user cadmate -- uv run --directory /path/to/cadmate cadmate
```

`--scope user` makes it available in every folder. Without it, `claude mcp add` registers the server for the current folder only, and a session started in your KiCad project's folder won't see it. A Claude Code session that is already running doesn't pick up a newly added server: start a new one.

cadmate builds and checks models itself with FreeCAD's command-line `freecadcmd` (the `environment` tool says where it found it), so FreeCAD installed is enough for generated models and for models Claude writes as a FreeCAD Python script and runs headless. A FreeCAD MCP server adds live modelling in the FreeCAD window, with screenshots along the way. The recommended one is [spkane/freecad-addon-robust-mcp-server](https://github.com/spkane/freecad-addon-robust-mcp-server) (MIT); follow its README. On macOS, run it against the FreeCAD app (socket/XML-RPC mode), not its embedded mode.

cadmate finds KiCad and FreeCAD in their usual install locations on macOS, Linux and Windows. To point it elsewhere, set these in the server's environment:

| Variable | Example |
| --- | --- |
| `CADMATE_KICAD_CLI` | `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` |
| `CADMATE_FREECADCMD` | `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd` |

KiCad path variables in model paths (for example `${KICAD10_3DMODEL_DIR}`) are expanded from the same environment. `${KIPRJMOD}` falls back to the footprint's folder.

## Skills

`skills/` holds the domain knowledge Claude loads for each workflow. Link the skill folders into `~/.claude/skills/` (or your project's `.claude/skills/`) to use them:

```sh
mkdir -p ~/.claude/skills
for s in /path/to/cadmate/skills/*/; do ln -s "$s" ~/.claude/skills/; done
```

To read datasheets, Claude needs a way to read PDFs and to look at drawings as images; `pip install pypdf cryptography pypdfium2 pillow` (or poppler's `pdftotext` and `pdftoppm`) covers both. `cryptography` is for the AES-encrypted PDFs some manufacturers publish (GCT's, for one), `pillow` for saving pypdfium2's page renders.

- `kicad-3d-models`: how to build, check and link a 3D model for a KiCad footprint.
- `kicad-footprints`: how to read a datasheet into a footprint, and review an existing one.
- `kicad-symbols`: how to read a datasheet's pin table into a symbol, and review an existing one.
- `kicad-parts`: how to make a whole part (symbol, footprint, model) in one go, and check an existing one.

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
