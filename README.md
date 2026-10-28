# cadmate

An AI companion for KiCad and FreeCAD: your CAD mate. It carries work across the two apps, footprints and boards from KiCad into FreeCAD and 3D models and enclosures back, and brings EE, PCB and 3D-printing know-how along.

cadmate is an [MCP](https://modelcontextprotocol.io) server. You talk to Claude (Claude Code or Claude Desktop). Claude uses cadmate for the KiCad side and the cross-app checks, and uses an existing FreeCAD MCP server for the modelling.

## Status

Early. Seven workflows work end to end:

- **Make the missing 3D model**: point Claude at a footprint and the part's datasheet, and get a checked STEP model linked into that footprint.
- **Make a footprint from a datasheet**: Claude reads the package drawing, cadmate computes IPC-7351B pads (or places the datasheet's recommended land pattern) and writes a checked `.kicad_mod`. For JEDEC packages the pads and courtyard match KiCad's own library footprints exactly.
- **Make a whole part from one datasheet**: symbol, footprint and 3D model from one reading of the datasheet, linked to each other (the symbol's Footprint field, the footprint's model link), checked together and registered in the project's library tables.
- **Work on a model together in FreeCAD**: Claude opens or builds the model in your FreeCAD window, you comment or edit it by hand, and Claude edits on top of what you changed, until you're both happy. Then it exports the STEP and checks it against the footprint. See "Live editing in FreeCAD" below.
- **Work on a footprint or symbol together in KiCad**: you keep it open in KiCad's footprint or symbol editor, save and comment or edit by hand, and Claude edits on top of what you saved; KiCad reloads it each time. See "Live editing in KiCad" below.
- **Work on a board together in KiCad's PCB editor**: Claude reads the board straight from your open window, saved or not, sees what you moved since its last look, and edits on top; each edit is one undo step in KiCad. See "Live editing in the PCB editor" below.
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
| `freecad_live_status` | Whether your FreeCAD window is reachable, and the line that starts the bridge if not | FreeCAD bridge |
| `freecad_live_state` | Objects in a document as they are now (properties, placement, exact bounding box) and what you edited by hand since Claude last looked | FreeCAD bridge |
| `freecad_live_edit` | Runs one edit in your window as one undo step named "cadmate: ..." | FreeCAD bridge |
| `freecad_live_open` | Opens an `.FCStd`, imports a STEP, or runs a model build script in your window | FreeCAD bridge |
| `freecad_live_snapshot` | A picture of the model from a standard view | FreeCAD bridge |
| `freecad_live_save` | Saves the document | FreeCAD bridge |
| `freecad_live_export_step` | Exports STEP with colours and runs the footprint check on it | FreeCAD bridge |
| `kicad_live_state` | A footprint or symbol as saved now, checked, and what you changed by hand since Claude last looked | nothing |
| `kicad_live_edit` | Edits a footprint or symbol on top of your saved edits, as one labelled step; KiCad reloads it | nothing |
| `kicad_live_undo` | Takes back Claude's last step, keeping your edits made since | nothing |
| `kicad_live_snapshot` | A picture of the footprint or symbol, plotted by kicad-cli | KiCad, and an SVG converter (macOS has one) |
| `kicad_pcb_state` | The board open in KiCad's PCB editor as it is now, your selection, and what you changed since Claude last looked | KiCad API on |
| `kicad_pcb_edit` | Edits the open board as one undo step named "cadmate: ..." | KiCad API on |
| `kicad_pcb_undo` | Takes back Claude's last step, only if nothing happened after it | KiCad API on |
| `kicad_pcb_save` | Saves the board | KiCad API on |
| `kicad_pcb_snapshot` | A picture of the open board, unsaved edits included | KiCad API on, and an SVG converter |

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

cadmate builds and checks models itself with FreeCAD's command-line `freecadcmd` (the `environment` tool says where it found it), so FreeCAD installed is enough for generated models and for models Claude writes as a FreeCAD Python script and runs headless. For working on a model together in the FreeCAD window, start cadmate's bridge (below); no addon is needed.

### Live editing in FreeCAD

FreeCAD has no built-in way for another program to work in a window that's already open, so cadmate ships a small bridge script that runs inside FreeCAD. It only runs when you start it:

1. Ask Claude for the start line (the `freecad_live_status` tool gives it). It looks like `exec(open('~/Library/Application Support/cadmate/cadmate_bridge.py').read())` with your home folder spelled out.
2. In FreeCAD, open View > Panels > Python console and paste it. The Report view shows `[cadmate] bridge listening on ...`.
3. To stop it, run `App._cadmate_bridge.stop()` in the same console, or quit FreeCAD. Running the start line again replaces a running bridge. It doesn't start with FreeCAD; to have it do that, add the start line to a macro of your own that FreeCAD runs at startup.

What it does to your document:

- Every command cadmate sends is printed in the Report view as `[cadmate] ...`, and errors in red.
- Every edit is one undo step named `cadmate: <what changed>`, so the undo list shows Claude's edits apart from yours and Ctrl+Z takes one back. An edit that fails is undone as a whole.
- Claude edits the objects that are there, it doesn't regenerate the document, so your hand edits stay. Before each edit it reads the bridge's change log, which records every change with whether it came from cadmate or from you.

The bridge listens on a Unix socket in a folder only your account can open (`~/Library/Application Support/cadmate/` on macOS, `$XDG_RUNTIME_DIR/cadmate/` on Linux), not on a network port, so a web page or another account can't reach it. Any program running as you can, the same as with your files, and what it sends is Python that FreeCAD runs: start the bridge when you want to work with Claude, and stop it afterwards if that matters to you. Windows isn't supported yet. `CADMATE_BRIDGE_SOCKET` moves the socket, in both FreeCAD's and the cadmate server's environment.

### Live editing in KiCad

KiCad's footprint and symbol editors have no API, but since KiCad 10 they reload the footprint or symbol they have open when its file changes on disk. So the loop works on the saved file:

1. Open the footprint or symbol in KiCad's editor from a library KiCad knows (the project's or the global library table).
2. Edit by hand if you want, then save (Cmd+S) and tell Claude what you'd like changed. Your unsaved edits aren't in the file, so Claude can't see them.
3. Claude reads the file, sees what you changed since its last look, and writes its edit on top. KiCad reloads it about a second later.

If you have unsaved edits when Claude writes, KiCad asks "The library containing the current footprint has changed. Do you want to reload?". Yes takes Claude's version and drops your unsaved edits; No keeps yours, and your next save overwrites Claude's step (Claude notices and asks before redoing it). Saving before you reply avoids both.

KiCad's undo list starts over on every reload, so Claude keeps its own: each step has a label, and Claude can take back its last one while keeping what you changed since. Steps are remembered across Claude sessions, in cadmate's data folder.

Footprints placed on a board don't follow along: use Tools > Update Footprints from Library in the PCB editor when you're done. In a symbol library, Claude only touches the one symbol; the rest of the file stays byte for byte.

### Live editing in the PCB editor

The PCB editor is the one part of KiCad with an API, so here the loop is properly live: no saving in between.

1. Turn the API on once: KiCad menu > Settings… (Preferences on Linux and Windows) > Plugins, tick "Enable KiCad API", and restart KiCad.
2. Open the board in the PCB editor and tell Claude what you want. Select parts to say "this one".
3. Claude reads the board from the editor, sees what you changed since its last look, and edits on top. Each edit is one undo step named `cadmate: <what changed>`, so Cmd+Z takes it back like any other.

The API has no change log, so cadmate tells your changes from its own by comparing the whole board with what it saw after its last edit. It asks Claude to undo its own step only when nothing happened after it, because KiCad's undo always takes back the last change, whoever made it. cadmate doesn't save the board unless you ask.

The API is reachable by any program running as you on this computer, the same as your files. Turn it off again in the same place if that matters to you.

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
6. Enclosure from the board, worked on live in FreeCAD
7. More live PCB help: DRC findings explained and fixed in the open board
8. Schematic help (live once KiCad 11's schematic API ships)
9. Buttons inside KiCad and FreeCAD that hand the current selection to Claude

## License

MIT. Tools under other licenses (for example AGPL tools such as KiBot) are only ever called as separate programs, never imported.
