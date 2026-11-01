# Getting started with cadmate

This guide takes you from nothing installed to a first design. The [README](../README.md) is the reference for each
tool; this is the order to do things in.

## What cadmate is

cadmate isn't an app you open. It's an [MCP](https://modelcontextprotocol.io) server: a small program that Claude
(Claude Code or Claude Desktop) starts in the background and calls as a set of tools. You talk to Claude in plain
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
  FreeCAD window. macOS and Linux only.

## Part 1: one-time setup

### 1. Install the prerequisites

| What | Why | Notes |
| --- | --- | --- |
| Python 3.11 or newer | cadmate is Python | CI tests 3.11 and 3.13 |
| [uv](https://docs.astral.sh/uv/) | installs and runs cadmate | |
| git | to clone the repo | |
| KiCad 10 | DRC, ERC, pictures, fab files, live PCB editing | older versions are reported as unsupported |
| FreeCAD 1.1 | 3D models, enclosure checks | only needed for 3D work |
| Claude Code or Claude Desktop | what you talk to | Claude Code is the tested path |
| Freerouting 2.5 or later, and Java | autorouting the non-critical nets | easiest through KiCad's Plugin and Content Manager; 2.2.4 fails on boards with locked tracks |
| PDF tools | so Claude can read datasheets | `pip install pypdf cryptography pypdfium2 pillow`, or poppler's `pdftotext` and `pdftoppm` |

What each OS has been tested with:

- **macOS**: the only OS used day to day, with KiCad 10.0.4 and FreeCAD 1.1. Everything works, including pictures
  (macOS has an SVG converter built in).
- **Linux**: CI runs the test suite, and runs cadmate's kicad-cli commands against a real KiCad 10. Nothing has been
  used by hand yet. For pictures install `rsvg-convert` (`librsvg2-bin` on Debian and Ubuntu). Flatpak and AppImage
  installs need a one-line wrapper script (see the README's Flatpak note).
- **Windows**: CI runs the test suite without KiCad or FreeCAD installed. Nothing has been used by hand. Known gaps: no
  live FreeCAD bridge (it needs a Unix socket), live KiCad editing reaches only the default KiCad instance, and
  pictures need the `snapshots` extra (step 2) plus the cairo DLL, for example from the GTK runtime.

### 2. Get cadmate

```sh
git clone https://github.com/arthursWizard/cadmate.git
cd cadmate
uv sync
uv run pytest
```

`uv sync` makes a private environment in `cadmate/.venv`. The tests pass with or without KiCad and FreeCAD; the ones
that need them are skipped.

On Windows, or on Linux without `rsvg-convert`, use `uv sync --extra snapshots` instead. It adds `cairosvg` for
pictures. Installing `cairosvg` by hand doesn't last: the next plain `uv sync` removes it.

### 3. Register cadmate with Claude

**Claude Code** (tested):

```sh
claude mcp add --scope user cadmate -- uv run --directory /full/path/to/cadmate cadmate
```

With the `snapshots` extra, put `--extra snapshots` after `uv run`. `--scope user` makes cadmate available in every
folder, not only the one you ran the command in. Start a new Claude Code session afterwards; one that's already running
doesn't see the new server. `/mcp` in a session shows whether cadmate is connected.

**Claude Desktop** (not tested yet): open Settings > Developer > Edit Config, add cadmate to
`claude_desktop_config.json`, and restart Claude Desktop:

```json
{
  "mcpServers": {
    "cadmate": {
      "command": "/full/path/to/uv",
      "args": ["run", "--directory", "/full/path/to/cadmate", "cadmate"]
    }
  }
}
```

Give `uv`'s full path (`which uv`, or `where uv` on Windows): desktop apps often don't see your shell's `PATH`. The
`CADMATE_*` variables from step 6 go in an `"env": {}` block beside `"args"`.

### 4. Install the skills

The skills are what make Claude follow IPC, KLC and the routing order rather than improvise. For Claude Code, link them
in:

```sh
mkdir -p ~/.claude/skills
for s in /full/path/to/cadmate/skills/*/; do ln -s "$s" ~/.claude/skills/; done
```

On Windows, copy the folders instead (`ln -s` needs developer mode there).

There are ten. `cadmate-project` (where files go) comes first; the others are `kicad-symbols`, `kicad-footprints`,
`kicad-3d-models`, `kicad-parts`, `kicad-schematics`, `kicad-pcb-live`, `kicad-pcb-route`, `kicad-pcb-review` and
`protoboard`.

Claude Desktop has its own way to add skills (uploaded in its settings) and may not read `~/.claude/skills`; neither has
been tried with these. The project rules reach Desktop either way, because cadmate sends them to every session that
connects.

### 5. Check what cadmate found

In a new session, ask Claude to run cadmate's environment check. The `environment` tool reports where it found KiCad,
kicad-cli, FreeCAD, Freerouting and Java, with their versions, and says what is too old, newer than tested, or missing.

### 6. Only if something wasn't found

cadmate looks for each program in this order: an environment variable, your `PATH`, then the usual install folders.
To point it somewhere else, set one of these in the server's environment:

`CADMATE_KICAD_CLI`, `CADMATE_FREECADCMD`, `CADMATE_FREEROUTING_JAR`, `CADMATE_JAVA`, `CADMATE_KICAD_PYTHON`

For Claude Code, add `-e NAME=value` to the `claude mcp add` line. The README lists example paths for each OS.

### 7. Optional: switch on the live connections

- **KiCad's PCB editor**: KiCad > Settings (Preferences on Linux and Windows) > Plugins, tick "Enable KiCad API",
  restart KiCad. Any program running as you can then reach the open board; turn it off the same way when that matters.
- **FreeCAD's window** (macOS and Linux): ask Claude for the bridge start line (`freecad_live_status` gives it) and
  paste it into FreeCAD's Python console (View > Panels > Python console). The Report view shows
  `[cadmate] bridge listening on ...`. It runs until you quit FreeCAD or run `App._cadmate_bridge.stop()`, and doesn't
  start by itself next time.

Without either, everything still works on saved files; you reload in KiCad or FreeCAD to see Claude's changes.

You don't need a separate FreeCAD MCP server or FreeCAD addon. cadmate builds and checks models headless with
`freecadcmd`, and works in your window through its own bridge.

## Part 2: how a project is laid out

Every design is one folder, the active project. There are no `board-v2` or `board-routed` copies: changes go in place,
and before a change worth undoing, Claude takes a snapshot into `history/`.

```
my-design/
  cadmate-project.json   README.md   CLAUDE.md
  kicad/<board>/         <board>.kicad_pro .kicad_sch .kicad_pcb ...
  kicad/lib/             your own footprints, symbols, 3D models
  case/                  FreeCAD files, STEP exports, enclosure.json
  fab/<board>/           Gerbers, BOM, CPL (generated, never edited)
  tools/                 generator scripts
  docs/brief.md          your requirements, as a checklist
  history/               20261031-1213_before-rework/ ... and HISTORY.md
```

The tools behind it: `project_init` makes this layout (safe on an existing folder, it only adds), `project_snapshot`
saves a state under a label, `project_history` lists the snapshots, `project_restore` brings one back (snapshotting the
current state first), and `project_check` lists anything out of place. Claude gets these rules every time it connects,
so you mostly say "snapshot this as before-rework" or "go back to yesterday's version".

## Part 3: your first session

1. Make an empty folder for the design and start Claude Code in it (`cd my-design && claude`).
2. Say what you're building, for example: "New design: a USB-C powered RP2040 board, 50 x 30 mm, two M3 holes, made at
   JLCPCB. Set it up as a cadmate project." Claude runs `project_init` and writes your requirements into
   `docs/brief.md`. Read that file and correct it now: every later step is checked against it.
3. Put the datasheets in `docs/` and say which parts you'll use.
4. Open the KiCad project Claude made, and keep KiCad open while you work.

## Part 4: the board flow

At every step Claude does the work and shows you a picture or a report. You look in KiCad and say "good" or what to
change. When you say good, Claude snapshots.

1. **Parts.** For anything not in KiCad's libraries: "make the part for U3 from docs/xyz.pdf". `generate_part` writes
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
   track width for the current, JLCPCB limits) and runs KiCad's DRC, reported as errors, warnings, unconnected and
   parity. Fix, and review again.
7. **Fab files.** `export_fab` writes the Gerber zip, BOM and CPL for JLCPCB into `fab/<board>/`, and lists the parts
   that have no LCSC number yet.
8. **Enclosure.** Claude builds the case in FreeCAD, live in your window on macOS and Linux, or headless from a script.
   The openings you want and the heights from the brief go in `case/enclosure.json`, and `check_enclosure` reports
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
