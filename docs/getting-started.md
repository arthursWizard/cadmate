# Getting started with cadmate

This guide takes you from nothing installed to a first design. The [README](../README.md) is the reference for each
tool; this is the order to do things in.

## What cadmate is

cadmate isn't an app you open. It's an [MCP](https://modelcontextprotocol.io) server: a small program that Claude
(Claude Code) starts in the background and calls as a set of tools. You talk to Claude in plain
language ("make a footprint for this SOIC-8 from the datasheet", "route this board", "check the case"). Claude reads your
files, calls cadmate's tools to generate, check and review KiCad and FreeCAD work, and shows you the result. You keep
KiCad and FreeCAD open beside it and look at, edit and approve what Claude did.

cadmate also ships skills: written know-how (IPC footprint rules, schematic drawing rules, the routing order) that
Claude reads before each kind of job.

Claude works on your design in three ways:

- **Files.** Footprints, symbols, schematics and boards are read and written as KiCad files. This works everywhere, and
  footprint and symbol work doesn't even need KiCad installed.
- **Live in KiCad's PCB editor.** With KiCad's API switched on, Claude reads and edits the board open in your window,
  each edit one undo step.
- **Live in FreeCAD.** With cadmate's bridge script running inside FreeCAD, Claude builds and edits models in your
  FreeCAD window, each edit one undo step. You start the bridge once by pasting a line Claude gives you, and from
  FreeCAD's Macro menu after that; see [step 5](#5-optional-switch-on-the-live-connections).

## Part 1: one-time setup

### 1. Install the prerequisites

| What | Why | Notes |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | installs, runs and updates cadmate | also fetches Python 3.11+ if you don't have it; CI tests 3.11 and 3.13 |
| KiCad 10 | DRC, ERC, pictures, fab files, live PCB editing | older versions are reported as unsupported |
| FreeCAD 1.1 | 3D models, enclosure checks | only needed for 3D work |
| Claude Code | what you talk to | cadmate installs as a Claude Code plugin |
| Freerouting 2.5 or later, and Java | autorouting the non-critical nets | easiest through KiCad's Plugin and Content Manager; 2.2.4 fails on boards with locked tracks |
| PDF tools | so Claude can read datasheets | `pip install pypdf cryptography pypdfium2 pillow`, or poppler's `pdftotext` and `pdftoppm` |

What each OS has been tested with:

- **macOS**: the only OS used day to day, with KiCad 10.0.4 and FreeCAD 1.1. Everything works, including pictures
  (macOS has an SVG converter built in).
- **Linux**: CI runs the test suite, and runs cadmate's kicad-cli commands against a real KiCad 10. Nothing has been
  used by hand yet. For pictures install `rsvg-convert` (`librsvg2-bin` on Debian and Ubuntu). Flatpak and AppImage
  installs need a one-line wrapper script (see the README's Flatpak note).
- **Windows**: CI runs the test suite without KiCad or FreeCAD installed. Nothing has been used by hand. The live FreeCAD
  bridge uses a localhost port with a token instead of a Unix socket and has only been tested in CI, without a real
  FreeCAD. Known gaps: live KiCad editing reaches only the default KiCad instance, and pictures need `cairosvg`, which
  the plugin doesn't install yet (step 2).

### 2. Install the cadmate plugin

There's no repo to clone. cadmate installs as a Claude Code plugin, which brings the MCP server and all ten skills
together, at the same version:

```sh
claude plugin marketplace add arthursWizard/cadmate
claude plugin install cadmate@cadmate
```

Or, inside a Claude Code session (2.1.275 or later), in one step: `/plugin install cadmate --marketplace arthursWizard/cadmate`.

Start a new session afterwards. `/mcp` shows whether cadmate is connected, `/plugin` lists the skills. The first start
takes a minute while uv fetches cadmate's dependencies; after that it's instant.

`cadmate@cadmate` is the released version. It only changes at a release, so what you have is something that has been
tried. To update: `claude plugin marketplace update cadmate`, then `claude plugin update cadmate@cadmate`, and start a
new session. Or let Claude Code do it: in `/plugin`, open Marketplaces, pick cadmate and choose Enable auto-update.

The ten skills are `cadmate-project` (where files go; read first), `kicad-symbols`, `kicad-footprints`,
`kicad-3d-models`, `kicad-parts`, `kicad-schematics`, `kicad-pcb-live`, `kicad-pcb-route`, `kicad-pcb-review` and
`protoboard`. They are what make Claude follow IPC, KLC and the routing order rather than improvise.

**Pictures on Windows, or on Linux without `rsvg-convert`**, need `cairosvg` (and the cairo library itself), which
the plugin doesn't install. On Linux, install `rsvg-convert` instead. On Windows, pictures don't work through the
plugin yet.

**Claude Desktop and other MCP clients** aren't a supported route yet. It should work by pointing the client at
`uvx --from git+https://github.com/arthursWizard/cadmate cadmate` (full path to `uvx`), but nobody has tried it.

**From source**, to work on cadmate itself:

```sh
git clone https://github.com/arthursWizard/cadmate.git
cd cadmate
uv sync
uv run pytest
claude plugin marketplace add ./
claude plugin install cadmate-dev@cadmate
claude plugin disable cadmate@cadmate    # only if you had the released one installed
```

The dev plugin then runs from your checkout, whichever branch it is on. The tests pass with or without KiCad and FreeCAD; the ones that need them
are skipped.

### Trying unreleased changes

The same marketplace has a second plugin, `cadmate-dev`, which runs whatever is on `main`: merged, passing CI, not yet
released. Use it to try a change before it reaches `cadmate@cadmate`.

```sh
claude plugin marketplace update cadmate
claude plugin install cadmate-dev@cadmate
claude plugin disable cadmate@cadmate
```

Start a new session. To get the newest `main` later, `claude plugin marketplace update cadmate` and
`claude plugin update cadmate-dev@cadmate`. To go back to the release:

```sh
claude plugin disable cadmate-dev@cadmate
claude plugin enable cadmate@cadmate
```

Keep only one of the two enabled. They have the same tool and skill names, so with both on you can't tell which one
Claude is using. `/plugin` shows which is enabled. [Releasing](releasing.md) has how the two are kept apart.

### 3. Check what cadmate found

In a new session, ask Claude to run cadmate's environment check. The `environment` tool reports where it found KiCad,
kicad-cli, FreeCAD, Freerouting and Java, with their versions, and says what is too old, newer than tested, or missing.


### 4. Only if something wasn't found

cadmate looks for each program in this order: an environment variable, your `PATH`, then the usual install folders.
To point it somewhere else, set one of these in the server's environment:

`CADMATE_KICAD_CLI`, `CADMATE_FREECADCMD`, `CADMATE_FREEROUTING_JAR`, `CADMATE_JAVA`, `CADMATE_KICAD_PYTHON`

Set them in the shell you start Claude Code from; the plugin's server inherits them. The README lists example paths for each OS.

### 5. Optional: switch on the live connections

- **KiCad's PCB editor**: KiCad > Settings (Preferences on Linux and Windows) > Plugins, tick "Enable KiCad API",
  restart KiCad. Any program running as you can then reach the open board; turn it off the same way when that matters.
- **FreeCAD's window**: the first time, ask Claude to start the FreeCAD bridge. It gives you a start line (from
  `freecad_live_status`) to paste into FreeCAD's Python console (View > Panels > Python console). The Report view shows
  `[cadmate] bridge listening on ...`. It runs until you quit FreeCAD or run `App._cadmate_bridge.stop()`, and doesn't
  start by itself next time. **Next time, don't paste anything:** that first start saved two macros, so use Macro >
  Macros... > `cadmate_bridge_start.FCMacro` > Execute (after that it's under Macro > Recent macros) to start it, and
  `cadmate_bridge_stop.FCMacro` to stop it. When FreeCAD work starts in a session, Claude tells you once whether the
  bridge is on and waits for you to switch it if you want to.

Without either, everything still works on saved files; you reload in KiCad or FreeCAD to see Claude's changes.

You don't need a separate FreeCAD MCP server or FreeCAD addon. cadmate builds and checks models headless with
`freecadcmd`, and works in your window through its own bridge.

## Part 2: how a project is laid out

Every design is one folder, the active project. There are no `board-v2` or `board-routed` copies: changes go in place,
and before a change worth undoing, Claude takes a snapshot into `history/`.

```
my-design/
  cadmate-project.json   README.md   CLAUDE.md
  kicad/                 main.kicad_pro .kicad_sch .kicad_pcb ...
  kicad/lib/             your own footprints, symbols, 3D models
  freecad/               enclosure.FCStd, STEP exports, enclosure.json
  fab/<board>/           Gerbers, BOM, CPL (generated, never edited)
  tools/                 generator scripts
  docs/brief.md          your requirements, as a checklist
  history/               20261031-1213_before-rework/ ... and HISTORY.md
```

That's a design with one board and one enclosure. No folder holds just one thing: a board-only design has
`main.kicad_pro`, `lib/` and `fab/` right in `my-design/`, and a FreeCAD-only one its `.FCStd` and STEPs. A second
board or part gets a folder each, named for what it is (`kicad/main/`, `kicad/sensor/`; `freecad/enclosure/`,
`freecad/knob/`). Claude moves things when that happens (`project_add_tool`, `project_add_board`, `project_add_part`),
after a snapshot; close the files in KiCad and FreeCAD first.

The tools behind it: `project_init` makes this layout (safe on an existing folder, it only adds), `project_snapshot`
saves a state under a label, `project_history` lists the snapshots, `project_restore` brings one back (snapshotting the
current state first), and `project_check` lists anything out of place. Claude gets these rules every time it connects,
so you mostly say "snapshot this as before-rework" or "go back to yesterday's version".

## Part 3: your first session

1. Make an empty folder for the design and start Claude Code in it (`cd my-design && claude`).
2. Say what you're building, for example: "New design: a USB-C powered RP2040 board, 50 x 30 mm, two M3 holes. Set it
   up as a cadmate project." Claude runs `project_init` and writes your requirements into `docs/brief.md`. It also asks
   you once where the board gets made: the board house, whether they assemble it, where you buy parts, how many
   layers. "Not decided yet" is a fine answer to any of them. The answers go into the brief's Fab section, and the
   review, routing and export steps use them. Read the brief and correct it now: every later step is checked against
   it.
3. Put the datasheets in `docs/` and say which parts you'll use.
4. Open the KiCad project Claude made, and keep KiCad open while you work.

## Part 4: the board flow

At every step Claude does the work and shows you a picture or a report. You look in KiCad and say "good" or what to
change. When you say good, Claude snapshots.

1. **Parts.** cadmate doesn't search distributors. For stock and price, add a search tool for where you buy:
   jlc-mcp (from anthropics/ai-eda) for JLCPCB and LCSC, or DigiKey's or Mouser's own API with your key. Put the
   manufacturer part number (and any distributor number) on each part; `generate_part` and `generate_symbol` take them
   as `fields`, and they end up in the BOM. For anything not in KiCad's libraries: "make the part for U3 from docs/xyz.pdf". `generate_part` writes
   the symbol, footprint and 3D model, linked and checked together, into `kicad/lib/`. `generate_symbol`,
   `generate_footprint` and the 3D model flow do one at a time. For existing parts, `check_part`, `check_footprint` and
   `review_footprint` catch swapped pads, wrong courtyards and models that don't sit right. To edit a footprint or
   symbol together, open it in KiCad's editor, save, and tell Claude; the footprint editor needs Refresh to show
   Claude's change, the symbol editor reloads by itself.
2. **Schematic.** KiCad 10 has no schematic API, so either you draw it and Claude reviews it, or Claude writes the
   `.kicad_sch` from a script in `tools/` and you tidy it. Either way `review_schematic` checks readability (overlaps,
   four-way junctions, ground symbols pointing up, sheets wired only by labels) and runs ERC.
3. **Placement.** Update the PCB from the schematic in KiCad, then place the parts by hand or with Claude in the live
   PCB editor ("put the decaps next to U1's power pins"). cadmate has no automatic PCB placement; placement is your call,
   with Claude's help.
4. **Critical connections.** "Classify the critical nets." `classify_critical_nets` lists decaps, crystals, USB pairs,
   regulator loops, ESD, analog, strap and high-current nets, each with its rule and source. You confirm, correct, or
   give currents ("VBUS carries 2 A").
5. **Routing.** `route_critical_nets` routes and locks the confirmed connections, `autoroute_rest` gives the rest to
   Freerouting, and `finish_routing` cleans up after Freerouting. Inside a project each changes the board in place
   after a snapshot; reload the board in KiCad when Claude says so.
6. **Review and DRC.** `review_pcb_layout` checks what a reviewer would (decap loops, crystal traces, return paths,
   track width for the current, and your board house's limits when you've chosen one cadmate knows: JLCPCB or PCBWay)
   and runs KiCad's DRC, reported as errors, warnings, unconnected and parity. Fix, and review again.
7. **Fab files.** Before ordering, "is it ready to order?": `check_ready_to_order` runs DRC with schematic parity,
   ERC, the layout review, part numbers, the fab files' age and the brief's open items together, and says what blocks
   the order. Then `export_fab` writes the Gerber zip, a BOM (manufacturer and part number, plus any LCSC, DigiKey or
   Mouser numbers) and a placement file into `fab/<board>/`, and lists the parts with no part number yet. With JLCPCB
   as your board house it also writes JLCPCB's own BOM and CPL.
8. **Enclosure.** `export_board_step` puts the board, with its parts' 3D models, into the FreeCAD part's `pcb-models/`
   and lists parts that have no model (they'd be missing from the collision check). Claude builds the case in FreeCAD, live in your window on macOS and Linux, or headless from a script.
   The openings you want and the heights from the brief go in `enclosure.json` beside the assembly, and `check_enclosure` reports
   collisions with the board and parts, openings nobody declared, and seams with a gap or a step. Fixing the case is a
   conversation in FreeCAD, not one tool.
9. **Done.** "Run project_check." It flags stale fab files, a missing or failing enclosure check, open items in the
   brief, and stray files.

## Part 5: the perfboard flow

For through-hole builds on a 2.54 mm perfboard, with no board to order:

1. Describe the circuit ("a 555 blinker on a 24 x 18 single-sided board"). Claude writes a design JSON with the board
   size, parts and nets; `examples/protoboard/555-blinker.json` is a whole one.
2. `place_protoboard` places the parts, steered by hints such as "connector on the left edge", tries a few placements
   and keeps the one that routes best. Or place by hand and run `check_protoboard`.
3. `route_protoboard` routes on the hole grid with solder bridges, bare wire and, where needed, insulated jumpers on
   top. It lists what it couldn't connect rather than make a mess.
4. `render_protoboard` draws the component side, the solder side mirrored as you'll see it turned over, and a build
   list to follow step by step.

Stripboard isn't supported yet.

## Good to know

- Only macOS has been used by hand. Linux and Windows rely on CI, which doesn't run KiCad's GUI or FreeCAD.
- Claude Desktop setup and skills are untested.
- cadmate doesn't save your board unless you ask, and never renames a KiCad project (that breaks the schematic's
  annotations).
- When something looks off, the two first things to ask for are the environment check and `project_check`.
