---
name: kicad-symbols
description: Make or review a KiCad schematic symbol from a part's datasheet. Use when the user asks for a symbol for a part, a .kicad_sym from a datasheet's pinout, or a check of a symbol they made or downloaded.
---

# KiCad symbol from a datasheet

Uses the cadmate MCP server. You read the pin table; cadmate lays the symbol out and checks it against KiCad's library conventions (KLC).

## First: does it need a new symbol?

KiCad's library has symbols for most common parts, often as derived symbols for each package (NE555D, NE555P). Look there first and say so if the part is in it. Generate one when it is not there, when the pinout differs (another package of the same chip often has other pin numbers), or when the user wants their own.

## Workflow

1. Find the **pin table** ("Pin Configuration and Functions", "Pin Descriptions") for the exact package the user will use. Pin numbers differ between packages; ask which package if it is not clear. Ask for the datasheet if you don't have it. Never guess a pin; ask.
2. Write one entry per package pin, in the table's order: `number`, `name`, `type`. Include every pin, NC pins and the exposed pad too, so the symbol matches the footprint.
   - Names as the datasheet writes them. Active-low: `~{RESET}` (cadmate converts `/RESET`, `!RESET` and `RESET#`; write `nRESET` or `RESET_N` as `~{RESET}` yourself when the datasheet means active-low).
   - Types: the table's I, O, I/O, P (power), G (ground), OD (open drain), NC work as they are. Choose the KiCad type with care, since ERC depends on it:
     - supplies and grounds: `power_in` (also for pins the datasheet calls "power" or "supply")
     - a regulator's or converter's output, or an LDO's output pin: `power_out`. An MCU's internal regulator output that feeds its own core (VREG_VOUT, VCAP, VDDCORE) is `power_out` only if it can supply other parts; a decoupling-only pin like VCAP is `passive`.
     - open-drain outputs (I2C, interrupts, PGOOD): `open_collector`
     - analogue inputs and outputs, crystal pins, feedback and compensation pins: `input` / `output` where the direction is clear, else `passive`
     - exposed pad: same name and type as the pin it connects to (usually GND, `power_in`); give it the footprint's pad number (often pins + 1).
3. Group pins by function with `group` (USB, QSPI, SWD, GPIO, crystal, control). A group stays together on one side with a gap before the next. cadmate places by type (inputs left, outputs right, supplies top, grounds bottom, a converter's input left and output right); set `side` on a group's pins when the layout reads better another way, e.g. all GPIO on the right, or a UART's TX and RX together.
4. Large parts: split into units with `unit` (for example one unit per port and one for power), or one unit per amplifier/gate plus a power unit, as KiCad's opamps do. Draw amplifier units as KiCad's triangle with `unit_shapes: {"1": "opamp", "2": "opamp"}`: each takes one + input, one - input and one output (shown as +, - and unnamed, like KiCad's), and optionally its supplies; put the supplies of a multi-amplifier part in their own rectangle unit. Comparators use the same shape.
5. `generate_symbol` into a project-local library (see "Files") with `description`, `keywords`, `datasheet` and `footprint` (Library:Name). Pass `footprint_path` with the `.kicad_mod` file when the footprint is local (one made with `generate_footprint`, for example): cadmate then checks that every pad has a pin and every pin a pad, and writes nothing if not.
6. Show the user the `report` (which pins went where, which pins stacked) and every warning. If KiCad's library has the same part, run `compare_symbols` against it and report the differences.
7. Ask the user to open the symbol in KiCad's symbol editor and confirm the pinout against the datasheet before using it.

## Connectors

KiCad's connector symbols (reference `J`) differ from ICs:

- Every pin is `passive`, supplies and grounds included: the connector passes power through, it neither takes nor drives it. cadmate accepts passive GND and VBUS on a `J` (or `P`) symbol.
- Pins that share a name stack, passive ones too, so a USB-C receptacle's four GND and four VBUS pins each become one pin (`[A1,A12,B1,B12]`), as in KiCad's `USB_C_Receptacle`. Give them the same name.
- The shield or shell is its own pin, `SHIELD` (KiCad's name), numbered as the footprint's shell pads (often `S1` or `SH`), at the bottom beside GND.
- Layout as KiCad's: VBUS first on the right, then the signals in their pairs (D+ and D- together, each SuperSpeed pair together, CC and SBU), GND and SHIELD at the bottom (`side: bottom`).
- A pin-numbered header (Conn_01x04) needs no generated symbol: use KiCad's generic one.

To review an existing symbol, run `check_symbol`, with `pins` from the datasheet to compare it pin by pin, and `footprint_path` to match its pins to the footprint's pads.

## Fields (KLC S6)

- `description`: what it is, then the package, like KiCad's: "USB serial converter, UART, SOIC-16", "1A Low Dropout regulator, positive, 3.3V fixed output, SOT-223". Don't repeat the part name.
- `keywords`: space-separated search words not already in the description, no punctuation: "USB UART serial converter". KiCad's library pairs some terms: with opamp add operational-amplifier, with LDO add low-dropout and regulator.
- Footprint filters: derived from `footprint`; for a part without a default footprint, pass `fp_filters` (e.g. `["SOIC*3.9x4.9mm*P1.27mm*", "DIP*W7.62mm*"]`) so KiCad offers the right footprints.
- `reference`: U for ICs (default); J connectors, Q transistors, D diodes, K relays, Y crystals, SW switches.
- `datasheet`: the URL. Footprint filters come from the footprint name unless you pass `fp_filters`.

## Conventions (applied by cadmate)

- mm, KiCad's symbol frame: Y points up.
- Pins end on the 100 mil (2.54 mm) grid; 100 mil long, longer for 3-character pin numbers, and 200 mil when the part has stacked pins so the braces KiCad draws round their numbers clear the body. Stacked numbers are written as a comma list, never a range: KiCad draws a range as one line along the pin, into the body. Body centred on the origin, 10 mil outline filled with the background colour, pin names 20 mil inside.
- Power and output pins that share a name become one pin numbered with all their pads, KiCad 10's native stack: IOVDD on pads 1, 10 and 22 is pin `[1,10,22]` (KLC S4.3). Pins that share a name but are passive (say two SW pins) stay separate.
- A chip with an on-chip regulator output (power_out) but many signals keeps its supplies on top; cadmate records that as a `KLC_S4.2_<pin>` exception field, as KiCad's own library does, since KiCad's checker would otherwise take the part for a power converter.
- NC pins are hidden, type no_connect, inside the body.
- Files are written in KiCad 10's format; cadmate needs KiCad 10 or later.

## Files

- Never write into KiCad's installed libraries.
- Project-local: `<project>/<name>.kicad_sym`, added in Preferences > Manage Symbol Libraries (Project tab).
- A personal library: a `.kicad_sym` file the user picks, added in the Global tab.
- `generate_symbol` adds the symbol to the library and leaves the other symbols untouched. It refuses to replace a symbol of the same name unless asked, and keeps the old library when it writes (in `history/` inside a cadmate project, else as `.bak`).

## Working live with the symbol open in KiCad

For iterating with the user: they look at it in KiCad's symbol editor, comment or edit by hand, and you edit on top. KiCad's symbol editor has no API, so cadmate works on the saved file.

1. The symbol must be open in KiCad's symbol editor from a library KiCad knows (the project's or the global sym-lib-table; `generate_part` registers its libraries). Ask the user to open it if it isn't.
2. Each round starts with the user saving in KiCad (Cmd+S on macOS, Ctrl+S elsewhere): their unsaved edits aren't in the file. Remind them once, at the start, then only when `user_changes` suggests they forgot.
3. `kicad_live_state` first, every round. Read `user_changes`: those are the user's hand edits since you last looked, pin by pin. Keep them and build on them; never revert one unless they ask. If one conflicts with the datasheet, say so and ask. When it reports `overwrote_cadmate_step`, a save in KiCad put back what your last edit or undo changed (`reverted` lists exactly what): ask the user before redoing it.
4. `kicad_live_edit` with small text replacements in the item's text as `kicad_live_state` returned it, one step per change you'd describe in one line, with that line as the label. Never regenerate the symbol or rewrite it whole: that throws away the user's edits. Fix the `errors` the reply reports, including ones the user's edits caused (a dragged pad past the courtyard), or tell the user why they stay.
5. `kicad_live_snapshot` and look at the picture before saying anything about the change. Then tell the user in a line what you changed, and that KiCad has reloaded it (about a second after the write; the reply's `kicad` line says what happens if their editor had unsaved edits).
6. `kicad_live_undo` takes back your last step and keeps their later edits; KiCad doesn't know your steps, so its undo can't.
