---
name: kicad-pcb-review
description: Review a routed KiCad board's layout, routed by hand or by an autorouter such as Freerouting. Use when the user asks to check, review or critique a board, its routing, decoupling, crystal, power traces or return paths, or to look over what an autorouter did.
---

# Reviewing a board's layout

Uses cadmate's `review_pcb_layout` tool. It reads the board open in KiCad's PCB editor, or a `.kicad_pcb` given by path, and returns findings. Each finding has a location, the reasoning and a source.

1. Before reviewing, find out what the board carries. The tool infers net roles from net names, pin types, power symbols and the parts on each net. Read `net_roles` in the reply. If a net is in the wrong role, or something fast sits under `signal` (an SPI clock named `SCK_2` gets caught, a net named `DATA` doesn't), run it again with `roles`.
2. Ask the user for the current on power nets, or read it from the datasheets (regulator output, motor or LED current), and pass it in `currents` (`{"+5V": 1.5}`). Without a current, power nets only report what their narrowest track can carry. That number is not a verdict.
3. The board house comes from the project (`project_fab`) unless you pass `fab`. cadmate never assumes one. With JLCPCB it adds JLCPCB's track, via and SMD spacing minimums; with PCBWay its track, via and edge minimums. With no house chosen, or one cadmate has no profile for, only KiCad's DRC with the board's own rules checks what can be made: `not_checked` says so, and say it to the user too. The reply's `fab` says which house was used and where that came from.
4. Present findings by what to do about them, worst first, with the location and the part references. Don't paste the JSON. Group repeats, for example one line for "five USB pairs cross the same pour gap" rather than five.
5. Say which thresholds are cadmate heuristics, not standards (`source: heuristic`; the numbers are in `limits`). If a finding conflicts with what the datasheet says, the datasheet wins. Tell the user so when it does.
6. Read `not_checked` too. "No crystal found" on a board that has one means the crystal's reference or footprint wasn't recognised. Pass its nets in `roles` as `crystal`.
7. Before the user orders, run `check_ready_to_order` on the saved board. It runs DRC with schematic parity, ERC, this review, part numbers, the fab files' age and the brief's open items together. Lead with ready or not, then every `blocking` item, then `to_look_at`. Ready needs DRC to have run: without kicad-cli it can't say yes.

## What the findings mean in practice

- **Decoupling.** The distance between a cap and its pin comes from placement, and a router can't fix it. Track length against the straight line, vias, and the loop through the ground pin come from routing. Autorouters typically get the routing part wrong: they connect the cap's power pad to the rail somewhere, not straight to the pin.
- **Crystal.** Short, on one layer, nothing alongside or underneath, ground under it. Load caps sit next to the crystal.
- **Return path.** Only fast edges (clocks, USB, SPI) care about a gap in the plane under them. On 2-layer boards, bottom-side tracks cutting the ground pour is the usual cause. Fix it by moving the cutting track, or stitch the gap with a ground track or vias.
- **Track width.** KiCad's DRC enforces the minimum width, not the net class width, so autorouted power nets often end up at the default width.

## With an autorouter

Push back on "autoroute everything, then fix it". Freerouting doesn't know which nets matter. Use the `kicad-pcb-route` skill: classify the critical connections, confirm them with the user, route and lock those, let Freerouting route the rest, then review here.

Re-review after every round of changes. Findings that persist across rounds are the ones to discuss with the user.

## Reporting DRC

Report DRC as four numbers every time: errors, warnings, unconnected and schematic parity, plus the warnings by type
(`review_pcb_layout` returns them under `drc`). "0/0/0" with the warnings left out hides work: warnings such as silkscreen
overlaps and dangling track stubs then only come up at the final clean-up. Say which warnings you'd leave
and why.
