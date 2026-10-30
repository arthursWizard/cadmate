---
name: kicad-schematics
description: Draw or tidy a KiCad schematic so a person can read it, and review a sheet's readability. Use when drawing a schematic, writing a .kicad_sch by script, or when the user asks to clean up, review or check a schematic.
---

# Readable KiCad schematics

Uses the cadmate MCP server (`review_schematic`). A schematic is read far more often than it is drawn; it should show how the circuit works at a glance, not only be electrically correct. ERC proves the connections; this skill and the review are about the reader.

## Rules

1. **Wires inside a block, labels between blocks.** Parts that depend on each other are wired: a regulator and its input and output caps, a crystal with its load caps and series resistor, a pull-up and the pin it pulls, a switch and its diode, ESD parts and the connector they protect. Use a net label where a net leaves the block (to the MCU, to a connector, to another sheet) or where a wire would cross half the sheet or other wires. A sheet where nearly every pin ends in a label is a netlist, not a drawing.
2. **Group into functional blocks.** Power input and regulation, USB, the MCU with its decoupling, flash, clock, reset and boot, debug, each interface, each connector. Give each block a short text heading ("3.3 V LDO", "12 MHz CRYSTAL (CL 20 pF)") and leave clear space between blocks.
3. **Signals flow left to right, power top to bottom.** Inputs and sources on the left, outputs and loads on the right; supplies point up, ground symbols point down and sit at the bottom of what they serve (KLC S4.1/S4.2 applies the same to symbols). Don't rotate a ground symbol to point up; route the wire down to it.
4. **No four-way junctions.** Offset one wire to make two T junctions; a lost junction dot turns a four-way into a crossing. Only T junctions get dots; wires that cross without a dot are not connected.
5. **Nothing overlaps.** No text over text, wires or other symbols; no symbols on top of each other. Keep each field upright and beside its part; for a row of parts, space them so their values fit (vertical 0402 caps with "100n" beside them need about 10 mm pitch).
6. **Fit the sheet.** Everything inside the border and clear of the title block. If a sheet gets crowded, use a larger sheet or split it into hierarchical sheets by block rather than shrinking spacing.
7. **On the grid.** Parts and wires on the 1.27 mm (50 mil) grid, pins on 2.54 mm, so wires meet pins exactly.
8. **Say what's not obvious.** A short note for choices a reviewer would question: why a value differs from the reference design, a part that is fitted only in one build, a pin-out that must match a mating board.
9. **Close every pin.** Unused pins get a no-connect flag; every power net that no part drives gets a PWR_FLAG, so ERC is clean.

When writing a .kicad_sch by script, escape line breaks in strings as `\n`: KiCad won't open a file with a raw line break inside quotes. The review flags it.

Judgement wins over the rules: if a wire makes a block harder to read, use a label and say why in a note.

## Workflow

1. Plan the blocks and where each sits on the sheet before placing parts: MCU in the middle, power top left, connectors at the edges facing outward, matrices and arrays as grids.
2. Place and wire one block at a time, labels only at its boundary.
3. Run `review_schematic` on each sheet. Fix every `problem` and `warning`; look at each `note`. The stats (`pins_by_wire`, `pins_by_label`, `two_terminal_by_label_only`) show whether the sheet reads as a drawing or a netlist.
4. Run ERC (the review does it when kicad-cli is found, otherwise `kicad-cli sch erc`) and fix every error.
5. Ask the user to open the sheet in KiCad: the review estimates text widths, so a person should look at the flagged spots and the sheet as a whole.

## Limits of the review

- Text widths are estimated from the font size (KiCad's stroke font has no metrics file), about ±15%.
- The title block size is approximate (KiCad's default, about 110 × 32 mm at the bottom right).
- It reviews one sheet at a time and doesn't follow hierarchical sheets.
- It doesn't judge layout quality beyond these checks: block grouping and signal flow still need a person's eye.
