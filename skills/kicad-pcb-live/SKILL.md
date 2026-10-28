---
name: kicad-pcb-live
description: Work on a board with the user while it is open in KiCad's PCB editor, through KiCad's API. Use when the user wants parts placed, moved or rotated, tracks or vias changed, or anything done "on my board" in KiCad, live.
---

# Working on a board live in KiCad's PCB editor

Uses cadmate's `kicad_pcb_*` tools. They work on the board in the user's open PCB editor, saved or not. Fewer rounds are better, but expect some.

1. `kicad_pcb_state`. If it fails with "can't reach KiCad's API", give the user the `how` line (turn on "Enable KiCad API" under KiCad's Settings > Plugins, restart KiCad) and wait. If KiCad has no board open, ask them to open it in the PCB editor.
2. Each round, `kicad_pcb_state` first. `user_changes` lists what the user changed since your last look or edit: keep it and build on it, never revert it unless they ask. `selection` is what they have selected: that is what "this", "these" or "that one" means.
3. `kicad_pcb_edit` with small Python edits through kicad-python: `place("C3", x=12.5, y=8)` for a footprint, or get items from `board`, change them and `board.update_items(...)`. One step per change you'd describe in one line, with that line as the label: the user sees it as "cadmate: <label>" in KiCad's undo list. Never delete and recreate the user's items to change them.
4. Check the reply's `changed` is what you meant. `kicad_pcb_snapshot` shows the whole board; look at it after placement changes before describing them.
5. Tell the user in a line what you changed. They can press Cmd/Ctrl+Z to take it back. `kicad_pcb_undo` does the same, and only works when nothing happened after your step.
6. Don't save (`kicad_pcb_save`) unless the user asks.

Moving a footprint doesn't reroute its tracks: say so when parts with tracks move, and ask before deleting or rerouting tracks. Zones need refilling after copper changes (`board.refill_zones()`); do it in the same step when a zone is affected.
