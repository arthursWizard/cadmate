# cadmate

An AI companion for KiCad and FreeCAD: your CAD mate. It carries work across the two apps, footprints and boards from KiCad into FreeCAD and 3D models and enclosures back, and brings EE, PCB and 3D-printing know-how along.

cadmate is an [MCP](https://modelcontextprotocol.io) server. You talk to Claude in Claude Code, and Claude calls cadmate's tools: KiCad files and the live PCB editor, FreeCAD models built headless with `freecadcmd` or live in your FreeCAD window through cadmate's own bridge, and the checks that tie the two together. No other FreeCAD MCP server or addon is needed.

New to cadmate? Start with [docs/getting-started.md](docs/getting-started.md): setup from scratch, the project layout, and a first design step by step.

## Status

Early. Ten workflows work end to end:

- **Make the missing 3D model**: point Claude at a footprint and the part's datasheet, and get a checked STEP model linked into that footprint.
- **Make a footprint from a datasheet**: Claude reads the package drawing, cadmate computes IPC-7351B pads (or places the datasheet's recommended land pattern) and writes a checked `.kicad_mod`. For JEDEC packages the pads and courtyard match KiCad's own library footprints exactly.
- **Make a whole part from one datasheet**: symbol, footprint and 3D model from one reading of the datasheet, linked to each other (the symbol's Footprint field, the footprint's model link), checked together and registered in the project's library tables.
- **Work on a model together in FreeCAD**: Claude opens or builds the model in your FreeCAD window, you comment or edit it by hand, and Claude edits on top of what you changed, until you're both happy. Then it exports the STEP and checks it against the footprint. See "Live editing in FreeCAD" below.
- **Work on a footprint or symbol together in KiCad**: you keep it open in KiCad's footprint or symbol editor, save and comment or edit by hand, and Claude edits on top of what you saved. The symbol editor reloads Claude's change by itself; the footprint editor needs a refresh. See "Live editing in KiCad" below.
- **Work on a board together in KiCad's PCB editor**: Claude reads the board straight from your open window, saved or not, sees what you moved since its last look, and edits on top; each edit is one undo step in KiCad. See "Live editing in the PCB editor" below.
- **Route a board, critical connections first**: cadmate finds the connections that need care before anything is routed: decap to pin, crystals, USB and other differential pairs, switching regulator hot loops, ESD protection, feedback and ADC nets, reset and strap nets, RF feeds, high-current paths and ground returns. Each comes with its rule and the source the rule comes from, and you confirm or change the list. cadmate routes those, locks only what it routed, lets Freerouting do the rest, then the review checks the result. See "Routing a board" below.
- **Review a schematic for readability**: text running over text or symbols, symbols on top of each other, items off the sheet or under the title block, four-way junctions, ground symbols pointing up, and a sheet that joins its resistors and caps by net labels instead of wires. The `kicad-schematics` skill carries the drawing rules (wires inside a functional block, labels where a net leaves it, signals left to right, power up and ground down). KiCad's ERC runs alongside when kicad-cli is found.
- **Review a routed board**: after routing by hand or with Freerouting, cadmate points out what a reviewer would. A decap whose track wanders to its pin or loops back through a distant ground. A crystal trace that is long, uses vias or has another signal alongside. Fast signals crossing gaps in the ground pour. Tracks narrower than their net class or than IPC-2221 asks for the current. JLCPCB's spacing and via limits. Each finding comes with its location, the reasoning and a source. See "Reviewing a layout" below.
- **Make a symbol from a datasheet**: Claude reads the pin table, cadmate lays out the symbol per KiCad's library conventions (grid, sides by pin type, stacked power pins, hidden NC pins, fields) and writes it into a `.kicad_sym` library, checked against the pin table and the footprint's pads.

- **Keep one active project with its history**: every design lives in one folder with a fixed layout (`kicad/<board>/`, `kicad/lib/`, `case/`, `fab/<board>/`, `tools/`, `docs/`; a KiCad-only or FreeCAD-only design drops the `kicad/` or `case/` level and keeps its files at the root). Changes go into it in place; when a state is worth keeping, `project_snapshot` copies it into `history/<YYYYMMDD-HHMM>_<label>/` first, and `project_restore` brings it back. The routing tools and the footprint, symbol and part writers follow this by themselves inside a project, and `project_check` points out anything out of place. See "Projects and history" below.

- **Check a printed case**: `check_enclosure` reads the case assembly in FreeCAD and reports what you'd otherwise find by eye or after printing: case parts colliding with boards, components or each other, a shell with an opening nobody asked for, and two case parts that don't meet flush (a gap along the seam, or one standing proud of the other). The openings meant to be there (buttons, USB, a display window) and the heights from the brief go in `case/enclosure.json`.

- **Lay out a circuit on perfboard**: through-hole parts on the 2.54 mm hole grid, each taking the holes its leads go through and covering the holes under its body. cadmate checks the placement, routes the nets with bottom-side solder bridges and bare wire and, where the bottom can't get through, insulated jumpers on top, and draws the board to copy by hand: the component side, the solder side mirrored as it looks turned over, and a build list. See "Perfboard layouts" below.

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
| `kicad_live_edit` | Edits a footprint or symbol on top of your saved edits, as one labelled step | nothing |
| `kicad_live_undo` | Takes back Claude's last step, keeping your edits made since | nothing |
| `kicad_live_snapshot` | A picture of the footprint or symbol, plotted by kicad-cli | KiCad, and an SVG converter (macOS has one) |
| `kicad_pcb_state` | The board open in KiCad's PCB editor as it is now, your selection, and what you changed since Claude last looked | KiCad API on |
| `kicad_pcb_edit` | Edits the open board as one undo step named "cadmate: ..." | KiCad API on |
| `kicad_pcb_undo` | Takes back Claude's last step, only if nothing happened after it | KiCad API on |
| `kicad_pcb_save` | Saves the board | KiCad API on |
| `classify_critical_nets` | The connections on a board that need care, by class, each with its rule, source, connections and what to do with it, as a table to confirm or override | nothing |
| `route_critical_nets` | Routes and locks the confirmed critical connections on the open board (one undo step) or into a copy of a `.kicad_pcb`; lists what it couldn't route and why | nothing |
| `autoroute_rest` | Freerouting on the rest, around the locked tracks, with IPC-2221 widths for nets with a current; brings the result back, widens tracks below the net class, refills zones | Freerouting's jar, java, KiCad's bundled Python |
| `review_schematic` | Readability review of a `.kicad_sch`: overlapping text and symbols, off-sheet items, four-way junctions, power symbol direction, label vs wire use; plus KiCad's ERC | nothing; kicad-cli for the ERC part |
| `review_pcb_layout` | Layout review of the open board or a `.kicad_pcb`: decoupling, crystal, switching nodes, return paths, width and current, vias, JLCPCB rules, and KiCad's DRC | nothing; kicad-cli for the DRC part |
| `list_protoboard_parts` | The perfboard parts library: pins in holes, covered holes, lead notes | nothing |
| `check_protoboard` | Checks a perfboard design: leads off the board or sharing a hole, leads under bodies, bodies overlapping, bad nets; and for wires: shorts, jumpers over parts, which nets are done | nothing |
| `place_protoboard` | Places the parts automatically, steered by hints (fixed, near a hole, a board region, an edge), routes several placements and keeps the best | nothing |
| `route_protoboard` | Routes a perfboard design on the hole grid (bottom, top on unplated double-sided boards, jumpers), keeping wires already drawn; lists what stays open | nothing |
| `render_protoboard` | Top and mirrored bottom views as PNG and SVG, plus a step-by-step build list | an SVG converter (rsvg-convert or cairosvg; macOS has one) |
| `project_init` | Makes a design folder a cadmate project with the fixed layout (flat for a KiCad-only or FreeCAD-only design); adds what's missing, moves nothing | nothing |
| `project_add_tool` | Adds KiCad or FreeCAD to a one-tool project: snapshots it, then moves the existing files from the root into `kicad/` or `case/` | nothing |
| `project_snapshot` | Copies the current state (or part of it) into `history/<YYYYMMDD-HHMM>_<label>/` before a change, with the libraries KiCad needs to open it | nothing |
| `project_history` | The snapshots, oldest first, with what each holds and why it was taken | nothing |
| `project_restore` | Puts a snapshot back into the active project, snapshotting the current state first | nothing |
| `project_check` | Everything out of place: stray versions and backups, wrong depth, misnamed boards, broken or absolute library paths, renamed-project annotations, stale fab files, a missing, stale or failing enclosure check | nothing |
| `check_enclosure` | Checks a case assembly: parts that collide, openings in each shell that aren't declared, joints between case parts with a gap or a step, part heights against the brief | FreeCAD |
| `kicad_pcb_snapshot` | A picture of the open board, unsaved edits included | KiCad API on, and an SVG converter |

## Version policy

cadmate requires **KiCad 10** and **FreeCAD 1.1** or later, and follows new releases rather than old ones: it writes the file formats of these versions, which later versions keep reading. `environment` reports an older install as unsupported, and a newer one than cadmate was tested with as `newer_than_tested`.

- **KiCad files** are read directly: footprints from KiCad 5 (`module`) to current (`footprint`) formats, symbol libraries from KiCad 6 on, including KiCad 10's one-file-per-symbol `.kicad_symdir` folders. They are written in KiCad 10's format, as KiCad's own editors write it. No KiCad install is needed for footprint or symbol work.
- **`kicad-cli`** for DRC, ERC, pictures and fab exports (Gerbers, drill, positions).
- **KiCad IPC API** (through `kicad-python`, pinned to the release that matches KiCad 10) only for live editing, switched on when present.
- **FreeCAD** through `FreeCADCmd`, using the Part API that is stable across 0.20 to 1.1. Headless builds keep their colours on FreeCAD 1.0 and later; with 0.20 and 0.21 the STEP is grey.

Everything tied to a version lives in `src/cadmate/versions.py`: the supported and tested versions, the file format numbers cadmate writes, and the kicad-cli arguments it uses. When a new KiCad or FreeCAD comes out, start there. CI runs cadmate's kicad-cli commands against a real KiCad (`tests/test_kicad_cli.py`), so a renamed flag or a changed report shows up as a failing check.

| | Tested with |
| --- | --- |
| KiCad | 10.0 (10.0.4 on macOS by hand; 10.0 on Linux in CI) |
| FreeCAD | 1.1 (macOS by hand) |
| Python | 3.11 and 3.13 in CI |

### Platforms

| | macOS | Linux | Windows |
| --- | --- | --- | --- |
| File work: footprints, symbols, parts, reviews | yes | yes | yes |
| kicad-cli checks and pictures | yes | yes | yes, untested by hand |
| FreeCAD models (FreeCADCmd) | yes | yes | yes, untested by hand |
| Live KiCad editing (IPC API) | yes | yes | the default KiCad instance only |
| Live FreeCAD bridge | yes | yes | yes, over a localhost port; CI only, untested by hand |
| Snapshots (SVG to PNG) | built in | needs `rsvg-convert` (librsvg), or the `snapshots` extra | needs the `snapshots` extra and a cairo DLL |
| Freerouting | yes | yes | yes, untested by hand |

CI runs the test suite on all three without KiCad or FreeCAD installed, and runs cadmate's kicad-cli commands against a real KiCad 10 on Linux. Day-to-day use so far has been on macOS only; Linux and Windows haven't been used by hand, and neither has Claude Desktop.

## Setup

Needs [uv](https://docs.astral.sh/uv/) (it fetches Python 3.11+ if you don't have it) and Claude Code. No clone needed:
cadmate installs as a Claude Code plugin, which brings the MCP server and the skills at the same version.

```sh
claude plugin marketplace add arthursWizard/cadmate
claude plugin install cadmate@cadmate
```

or, in a session, `/plugin install cadmate --marketplace arthursWizard/cadmate`. Update with
`claude plugin marketplace update cadmate && claude plugin update cadmate@cadmate`, or turn on auto-update for the
marketplace in `/plugin`. Start a new session after installing or updating.

`cadmate@cadmate` is the latest release. To try what is merged on `main` but not released, install `cadmate-dev@cadmate`
from the same marketplace and disable `cadmate@cadmate`; keep only one of the two enabled. See
[Getting started](docs/getting-started.md#trying-unreleased-changes) and [Releasing](docs/releasing.md).

For pictures on Linux, install `rsvg-convert` (macOS has a converter built in). The plugin doesn't install
`cairosvg`, so pictures on Windows don't work through it yet.

Claude Desktop and other MCP clients aren't a supported route yet; pointing one at
`uvx --from git+https://github.com/arthursWizard/cadmate cadmate` should work but hasn't been tried.

From source, to work on cadmate itself:

```sh
git clone https://github.com/arthursWizard/cadmate.git && cd cadmate
uv sync
uv run pytest
claude plugin marketplace add ./ && claude plugin install cadmate-dev@cadmate   # runs from your checkout
```

cadmate builds and checks models itself with FreeCAD's command-line `freecadcmd` (the `environment` tool says where it found it), so FreeCAD installed is enough for generated models and for models Claude writes as a FreeCAD Python script and runs headless. For working on a model together in the FreeCAD window, start cadmate's bridge (below); no addon is needed.

### Live editing in FreeCAD

FreeCAD has no built-in way for another program to work in a window that's already open, so cadmate ships a small bridge script that runs inside FreeCAD. It only runs when you start it:

1. Ask Claude for the start line (the `freecad_live_status` tool gives it). It looks like `exec(open('~/Library/Application Support/cadmate/cadmate_bridge.py', encoding='utf-8').read())` with your home folder spelled out (on Windows the folder is `%LOCALAPPDATA%\cadmate`).
2. In FreeCAD, open View > Panels > Python console and paste it. The Report view shows `[cadmate] bridge listening on ...`.
3. To stop it, run `App._cadmate_bridge.stop()` in the same console, or quit FreeCAD. Running the start line again replaces a running bridge. It doesn't start with FreeCAD; to have it do that, add the start line to a macro of your own that FreeCAD runs at startup.

What it does to your document:

- Every command cadmate sends is printed in the Report view as `[cadmate] ...`, and errors in red.
- Every edit is one undo step named `cadmate: <what changed>`, so the undo list shows Claude's edits apart from yours and Ctrl+Z takes one back. An edit that fails is undone as a whole.
- Claude edits the objects that are there, it doesn't regenerate the document, so your hand edits stay. Before each edit it reads the bridge's change log, which records every change with whether it came from cadmate or from you.

On macOS and Linux the bridge listens on a Unix socket in a folder only your account can open (`~/Library/Application Support/cadmate/` on macOS, `$XDG_RUNTIME_DIR/cadmate/` on Linux), not on a network port, so a web page or another account can't reach it.

Windows has no Unix sockets in Python, so there the bridge listens on a port on `127.0.0.1` (never on the network) and writes the port and a random token, new each time it starts, to `bridge.json` in `%LOCALAPPDATA%\cadmate`, which only your account can read. cadmate reads that file and sends the token with every request; the bridge refuses anything without it, so a web page or another account that finds the port gets nothing. Setting `CADMATE_BRIDGE_TRANSPORT=tcp` in FreeCAD's environment does the same on macOS or Linux, for a FreeCAD that can't use the socket; the cadmate server follows `bridge.json` when it's there, so it needs no setting.

Either way, any program running as you can reach the bridge, the same as with your files, and what it sends is Python that FreeCAD runs: start the bridge when you want to work with Claude, and stop it afterwards if that matters to you. `CADMATE_BRIDGE_SOCKET` moves the socket and cadmate's data folder with it, in both FreeCAD's and the cadmate server's environment.

### Live editing in KiCad

KiCad's footprint and symbol editors have no API, so this loop works on the saved file:

1. Open the footprint or symbol in KiCad's editor from a library KiCad knows (the project's or the global library table).
2. Edit by hand if you want, then save (Cmd+S) and tell Claude what you'd like changed. Your unsaved edits aren't in the file, so Claude can't see them.
3. Claude reads the file, sees what you changed since its last look, and writes its edit on top. It shows you a picture of the result.

Seeing Claude's change in KiCad (checked on KiCad 10.0.4, macOS):

- Footprint editor: click Refresh in its toolbar, then double-click the footprint in the library tree.
- Symbol editor: nothing to do; it reloads the symbol about a second after Claude writes. If you had unsaved edits it asks first: yes takes Claude's change and drops yours, no keeps yours.

Until the footprint editor shows the new version (or if you answer no in the symbol editor), saving from it writes over Claude's change. Claude notices when that happens and asks before redoing it.

KiCad doesn't know about Claude's steps, so Claude keeps its own list: each step has a label, and Claude can take back its last one while keeping what you changed since. Steps are remembered across Claude sessions, in cadmate's data folder.

Footprints placed on a board don't follow along: use Tools > Update Footprints from Library in the PCB editor when you're done. In a symbol library, Claude only touches the one symbol; the rest of the file stays byte for byte.

### Live editing in the PCB editor

The PCB editor is the one part of KiCad with an API, so here the loop is properly live: no saving in between.

1. Turn the API on once: KiCad menu > Settings… (Preferences on Linux and Windows) > Plugins, tick "Enable KiCad API", and restart KiCad.
2. Open the board in the PCB editor and tell Claude what you want. Select parts to say "this one".
3. Claude reads the board from the editor, sees what you changed since its last look, and edits on top. Each edit is one undo step named `cadmate: <what changed>`, so Cmd+Z takes it back like any other.

The API has no change log, so cadmate tells your changes from its own by comparing the whole board with what it saw after its last edit. It asks Claude to undo its own step only when nothing happened after it, because KiCad's undo always takes back the last change, whoever made it. cadmate doesn't save the board unless you ask.

The API is reachable by any program running as you on this computer, the same as your files. Turn it off again in the same place if that matters to you.

### Routing a board

The flow: place the parts, classify the critical connections and confirm them, route those first and lock them, let Freerouting route the rest, review, fix, and review again.

`classify_critical_nets` sorts what it finds into classes. Each class is a detector with its rule and sources (`pcb_critical.py`); adding a class is adding a detector.

| Class | Rule | Source |
| --- | --- | --- |
| RF and antenna feed | 50 Ω, short, no vias, ground stitching, nothing nearby. Routed by hand. | Espressif ESP hardware design guidelines |
| Switching regulator hot loop | Input cap at VIN and PGND, no vias in the loop, inductor at SW, output cap at the inductor | ADI AN-139, TI AN-1149 |
| Decoupling cap to pin | Nearest small cap straight to the pin on one layer, ground into the pour at the pad | ADI MT-101 |
| Crystal or oscillator | Short, one layer, no vias, load caps at the crystal, nothing under it | ST AN2867 |
| Differential pair | USB 2.0 90 Ω, 50 mil skew, ≤ 4 vias per trace; PCIe/HDMI 100 Ω; routed together, through ESD pads | TI SPRAAR7 |
| ESD/TVS protection | TVS at the connector, line through the TVS pad, one via at its ground pin | TI SLVA680 |
| Sensitive analog | Feedback, ADC, reference and sense parts at the pin, away from switching nodes and clocks | ADI MT-031, TI AN-1149 |
| Reset, boot and strap | Pull and filter parts at the pin, short track | the datasheet; cadmate heuristic |
| High-current path | IPC-2221 width for the current, given to Freerouting as a net class | IPC-2221 |
| Ground return and pours | Fast nets over unbroken ground, checked after routing | Altium; TI SPRAAR7 |

Each item says what to do with it: `route` (cadmate routes and locks it), `place` (a part is too far for routing to fix), `hand`, `netclass`, `ask` (needs a current) or `check`. `overrides` changes it.

`route_critical_nets` is a grid router for short critical connections: clearances from the net classes, checked again exactly on the final tracks, pairs routed as one and split at width and gap, ground vias placed after the tracks so they don't wall them in. What it can't route within the rule it lists with the reason, rather than route badly. On KiCad's StickHub demo with all tracks removed, it routes 79 of 82 critical connections in about 10 seconds. Pairs whose two ends are on different layers through separate TVS parts can still come out a few mm apart in length; tune those with KiCad's skew tuner.

`autoroute_rest` exports a DSN with KiCad's own exporter (in KiCad's bundled Python; kicad-cli has no DSN export), runs Freerouting headless, and reads its session file itself, so the result goes into the open board as one undo step. Lock only the critical tracks: locking whole power nets boxes Freerouting in. Freerouting 2.2.4 throws on boards with locked tracks; use 2.5 or later.

### Reviewing a layout

`review_pcb_layout` reads the board from the PCB editor (unsaved edits included) or from a `.kicad_pcb`, and from the project file and schematic next to it. The project file gives the net classes. The schematic gives the power symbols, which help tell what each net is.

- **Decoupling.** For each IC power pin it takes the nearest bypass cap on that net, and checks how far the cap is placed, how long the track to the pin is compared to the straight line, the vias on the way, and the loop back through the IC's ground pin. Source: ADI MT-101.
- **Crystal.** Track length and vias to the IC, other signals running alongside or through its area, load caps, and ground under it. Source: ST AN2867.
- **Switching nodes.** Signals running next to a regulator's SW/LX net, feedback especially. Source: TI AN-1149.
- **Return path.** Signals crossing a gap in the ground pour on the layer next to them. These are warnings on fast nets and grouped notes on others. Source: Altium's guidance.
- **Width and current.** Tracks narrower than their net class, which KiCad's DRC doesn't check. For the nets you give currents for, width per IPC-2221.
- **Vias.** Count per net, and sizes against JLCPCB's minimums.
- **Assembly.** Package-to-package spacing from JLCPCB's SMD table, and parts close to the edge.
- **KiCad's DRC.** Unrouted nets and clearances, when `kicad-cli` is found.

It infers what each net carries (ground, power, switching node, crystal, fast signal) from net names, the pads' pin types, power symbols and the parts on the net, and says how it decided. When it can't tell, it says so; pass `roles` to correct it. Numbers no standard sets (for example, how far is too far for a decap) are cadmate's own heuristics, listed in the reply under `limits`, and findings based on them cite `heuristic`. Treat them as prompts to look, not as pass or fail.

cadmate looks for each program in this order: the environment variable below, the `PATH`, then the usual install folders for the platform (`/Applications` and `~/Applications` on macOS; `/usr/bin`, `/usr/local/bin` and the FreeCAD snap on Linux; `%ProgramFiles%` and per-user `%LOCALAPPDATA%\Programs` on Windows). Of several installed versions it takes the newest. To point it elsewhere, set these in the server's environment:

| Variable | macOS example | Linux example | Windows example |
| --- | --- | --- | --- |
| `CADMATE_KICAD_CLI` | `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` | `/usr/bin/kicad-cli` | `C:\Program Files\KiCad\10.0\bin\kicad-cli.exe` |
| `CADMATE_FREECADCMD` | `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd` | `/usr/bin/freecadcmd` | `C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe` |
| `CADMATE_FREEROUTING_JAR` | `~/Documents/KiCad/10.0/3rdparty/plugins/app_freerouting_kicad-plugin/jar/freerouting-2.5.0.jar` | `~/.local/share/kicad/10.0/3rdparty/plugins/…/freerouting-2.5.0.jar` | `~\Documents\KiCad\10.0\3rdparty\plugins\…\freerouting-2.5.0.jar` |
| `CADMATE_JAVA` | `/opt/homebrew/opt/openjdk/bin/java` | `/usr/lib/jvm/java-21-openjdk-amd64/bin/java` | `C:\Program Files\Eclipse Adoptium\jdk-21\bin\java.exe` |
| `CADMATE_KICAD_PYTHON` | `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3` | `/usr/bin/python3` (the distro's KiCad installs `pcbnew` there) | `C:\Program Files\KiCad\10.0\bin\python.exe` |

`environment` says when a variable is set but points at nothing.

Flatpak and AppImage installs have no `kicad-cli` or `freecadcmd` file to point at. Make a one-line wrapper script and point the variable at it, for example for KiCad from Flathub:

```sh
#!/bin/sh
exec flatpak run --command=kicad-cli org.kicad.KiCad "$@"
```

Flatpak's sandbox only sees some of your folders, so keep projects under your home folder.

KiCad path variables in model paths (for example `${KICAD10_3DMODEL_DIR}`) are expanded from the same environment. `${KIPRJMOD}` falls back to the footprint's folder.

### Perfboard layouts

A perfboard design is a JSON file: the board size in holes, the parts with the hole of pin 1 ("C5": row letter, column number, A1 top left from the component side) and a rotation, and the nets. `examples/protoboard/555-blinker.json` is a whole one. Parts come from a small library (`list_protoboard_parts`: axial resistors and diodes, ceramic and electrolytic caps, LEDs, DIPs, headers, TO-92, TO-220, screw terminals, tact switches, trimmers, crystals) or are given inline with their pins and body.

The board's `pads` says what kind it is: `single` (pads on the bottom), `double` (both sides, plated through) or `double-unplated` (both sides, not joined). On a plated board a hole's two pads are one node, so the top side gives no extra routing room, only sturdier pads; the router keeps to the bottom. On an unplated board the two pads are separate, so a top track can pass over a hole whose bottom pad another net uses. Where a net changes side away from a lead, the build list asks for a via wire: a bit of bare wire through the hole, soldered both sides. Check your board with a meter (top pad to bottom pad of an empty hole) before choosing.

The router works on holes. A pad belongs to one net, and wires run pad to pad along rows and columns, under bodies on the bottom, never under them on top. A jumper is a straight insulated wire on the component side whose two ends go down free holes; it can't lie over a lead, a body, a top track or another jumper. Nets are routed together, PathFinder style: a hole two nets want gets dearer each round until they share none. Wires already in the design are kept, so you can lay a ground bus by hand and let it route the rest. Nets still open after that get a rip-up-and-reroute pass. On a crowded board it lists what it couldn't connect instead of making a mess.

`place_protoboard` places parts for you. Parts steer it with rough hints: `"fixed": true` with `at`, `at` alone as a starting point, `"near": "C5"`, `"region": "left"` (or right, top, bottom, a corner, center, or `edge` for connectors), `"keep_rotation": true`. It keeps nets short and leaves room between pins of different nets, then places a few times, routes each, and keeps the one that routes best. On the 555 example with nothing placed it finds a fully routed layout in about two seconds. Hand placement still wins on crowded boards, so the intended loop is: place, look at the picture, nudge with hints, place again.

`render_protoboard` writes `<name>.png`, `<name>.svg` and `<name>-build.md`. The bottom view is mirrored the way the board looks when you turn it over, with the same row letters and column numbers, so a hole is called the same on both sides.

Stripboard (copper strips you cut) isn't supported yet.

## Projects and history

A design is one folder, the active project. Everything changes in place; history is a folder of snapshots, not
`-v2` copies beside the files.

```
<project>/
  cadmate-project.json  README.md  CLAUDE.md
  kicad/<board>/<board>.kicad_pro|.kicad_sch|.kicad_pcb|.kicad_dru, fp-lib-table, sym-lib-table
  kicad/lib/            project footprints, symbols and 3D models (${KIPRJMOD}/../lib/...)
  case/                 FreeCAD sources, <part>.step, assembly.step, renders/, pcb-models/
  fab/<board>/          generated fab files
  tools/                generator scripts: the design's source
  docs/                 datasheets, drawings, notes
  history/HISTORY.md    and 20261031-1213_before-rework/ ... (same paths as the project)
  .cadmate/work/        scratch
```

That's the shape with both KiCad and FreeCAD. A design with only one of them is flat: no `kicad/` or `case/`, so a
KiCad-only project has `<board>/`, `lib/` and `fab/` at the root, and a FreeCAD-only one has its `.FCStd` and STEP
files there. `cadmate-project.json` records the tools; pass `tools` to `project_init`. When a flat project gains the
other tool, `project_add_tool` snapshots it and moves the files into `kicad/` or `case/`.

Inside a project, `route_critical_nets`, `autoroute_rest` and `finish_routing` change the board itself after
snapshotting it (`in_place=False` sends the result to `.cadmate/work/` instead), `export_fab` writes to `fab/<board>/`
and replaces the old Gerbers, and the footprint, symbol and part writers keep replaced files in `history/` rather than
as `.bak`. Outside a project they behave as before. The server's instructions carry the rules to every session that
connects, and the `cadmate-project` skill has the reasons and the edge cases (library paths, renaming, open files).

## Skills

`skills/` holds the domain knowledge Claude loads for each workflow. The plugin installs them. Without the plugin, `cadmate install-skills` copies them into `~/.claude/skills/` (or `--dest <project>/.claude/skills`), replacing only folders it installed.

To read datasheets, Claude needs a way to read PDFs and to look at drawings as images; `pip install pypdf cryptography pypdfium2 pillow` (or poppler's `pdftotext` and `pdftoppm`) covers both. `cryptography` is for the AES-encrypted PDFs some manufacturers publish (GCT's, for one), `pillow` for saving pypdfium2's page renders.

- `cadmate-project`: where files go, when to snapshot, how to try alternatives and clean up a sprawled project. Read it first.
- `kicad-3d-models`: how to build, check and link a 3D model for a KiCad footprint.
- `kicad-footprints`: how to read a datasheet into a footprint, and review an existing one.
- `kicad-symbols`: how to read a datasheet's pin table into a symbol, and review an existing one.
- `kicad-parts`: how to make a whole part (symbol, footprint, model) in one go, and check an existing one.
- `kicad-pcb-live`: working on a board in the open PCB editor.
- `kicad-pcb-route`: routing a board with the user: critical connections first, then Freerouting, then review.
- `kicad-pcb-review`: reviewing a routed board, and how to use an autorouter without letting it decide the critical nets.
- `protoboard`: laying out, routing and drawing a through-hole circuit on perfboard.

## Roadmap

1. Missing 3D model for a footprint (done)
2. Footprints from datasheets (gull-wing, QFN/DFN, custom land patterns, through-hole holes and slots sized from the leads; SOT, chip parts and DIP/header families next)
3. Symbols from datasheets (rectangular and opamp bodies, multi-unit, alternate pin functions; logic gate shapes next)
4. Board review: layout review (done: decoupling, crystal, return paths, width and current, JLCPCB rules); ERC with explanations and fixes next
5. Fab export per fab house
6. Enclosure from the board, worked on live in FreeCAD
7. More live PCB help: DRC findings explained and fixed in the open board
8. Schematic help (live once KiCad 11's schematic API ships)
9. Buttons inside KiCad and FreeCAD that hand the current selection to Claude

## License

MIT. Tools under other licenses (for example AGPL tools such as KiBot) are only ever called as separate programs, never imported.
