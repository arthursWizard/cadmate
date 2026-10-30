---
name: kicad-pcb-route
description: Route a KiCad board with the user, critical connections first. Use when the user asks to route, autoroute or Freeroute a board, or asks which nets need care (decoupling, crystal, USB or other pairs, regulator loops, RF, high current) before routing.
---

# Routing a board: critical connections first, then Freerouting, then review

Uses cadmate's `classify_critical_nets`, `route_critical_nets`, `autoroute_rest` and `review_pcb_layout`. Each works on the board open in KiCad's PCB editor (leave `path` out) or on a `.kicad_pcb` (results go to a new file, the original is kept).

Push back on "autoroute everything". Freerouting doesn't know which connections matter, and fixing its decap and pair routing afterwards takes longer than routing those first.

1. Placement first. If the parts aren't placed, stop: routing can't fix placement.
2. `classify_critical_nets`. Show the user the `table`, grouped by class, worst placement problems first. Say which items cadmate will route, which are placement problems (`place`), which need a number (`ask`: currents for power nets), and which are left to the review (`check`). Name the classes that found nothing (`not_found`) so the user can say if one is missing, for example a feedback net cadmate didn't recognise. Wait for the user to confirm or change it:
   - an item's action: `overrides` `{item id: "route" | "hand" | "skip" | "place"}`
   - what a net is: `roles` `{net: "fast" | "sensitive" | "switching" | ...}`
   - currents: `currents` `{net: amps}`, from the user or the datasheets (regulator output, motor, LED, USB ports × 0.5 A or 0.9 A).
   Quote the sources (`sources`) when the user asks why. Numbers marked `heuristic` are cadmate's own.
3. Fix `place` items first, with the user or `kicad_pcb_edit`, then classify again.
4. `route_critical_nets` with the same `roles`, `currents` and `overrides`. It routes the `route` items, locks only what it added, and lists `by_hand` connections with the reason. An item that failed or broke its rule is retried routed first (`routed_first`). `over_rule` lists what is routed but outside its rule: a trace longer than its limit, or a pair whose `mismatch_mm` is over the skew limit. Don't call those done: for a pair, suggest KiCad's skew tuner (Route > Tune Skew of a Differential Pair) on the shorter net; for a long trace, a placement change or routing it by hand. Look at the board with `kicad_pcb_snapshot`.
5. Ask the user to route the `by_hand` items and the `hand` items (RF lines need the 50 Ω width from the stackup), or leave them to Freerouting if they aren't critical after all.
6. Ask which fab the board goes to (JLCPCB or PCBWay) if you don't know. A fab's minimum is a floor, not a target: cadmate adds a 25 % buffer (`margin`). `autoroute_rest` with the same `currents` and `fab`. `fab_check.errors` are tracks under the fab's minimum: reroute them or route them by hand. `fab_check.warnings` are between the minimum and the buffer, usually Freerouting's necks into small pads: list them for the user to keep or fix. Necks into pads get a KiCad custom rule (`neck_rules`, in the board's .kicad_dru) so DRC stops flagging them; the rest stay DRC errors to fix. It doesn't lock anything else: locking whole power nets boxes Freerouting in. Ground and big power nets are better as pours. Before routing it adds keepouts along the board outline (inner cutouts included) and around every non-plated hole, at the board's edge and hole clearance rules (the fab's preferred edge value if larger), since Freerouting keeps no clearance to the outline by itself (`keepouts`). `complete` is false when anything is left unrouted: run again with more `passes`, or route the rest by hand; `next` says which. `clearance_check` lists new copper under the edge or hole clearance; `dangling_vias_removed` counts vias Freerouting left touching nothing. Read `widened` (tracks Freerouting made narrower than the net class, put back to width where clearance allows), `still_narrow` (where it didn't: these show up in DRC as track width errors if below the board minimum; fix by hand), `widths_capped` (high-current nets the autorouter couldn't make wide at a small pad) and `unrouted`.
7. `review_pcb_layout` with the same `currents`. Fix what it finds, and route again where needed. Repeat until the review has nothing the user cares about.

Every step on the open board is one undo step in KiCad, and nothing is saved until the user saves. Tell the user that after each step.

## What each class asks for

- **RF/antenna feed**: 50 Ω, short, no vias, ground stitching (Espressif's layout guide). Routed by hand.
- **Switching regulator hot loop**: input cap at VIN and PGND, no vias in the loop, inductor at SW, output cap at the inductor (ADI AN-139, TI AN-1149).
- **Decoupling**: each power pin's nearest small cap, straight to the pin on one layer, ground into the pour with a via at the pad (ADI MT-101).
- **Crystal**: short, one layer, no vias, load caps at the crystal, nothing routed under it (ST AN2867).
- **Differential pairs**: USB 2.0 90 Ω, 50 mil skew, at most 4 vias per trace; PCIe and HDMI 100 Ω (TI SPRAAR7). Routed together, through ESD pads, not past them. Other pair types use cadmate's numbers.
- **ESD/TVS**: at the connector, the line goes through the TVS pad, one via at the TVS ground pin (TI SLVA680).
- **Sensitive analog**: feedback, ADC, reference and sense parts at their pin, away from switching nodes and clocks (ADI MT-031, TI AN-1149).
- **Reset/boot/strap**: pull and filter parts at the pin (datasheet).
- **High current**: IPC-2221 width for the current, given to Freerouting as a net class.
- **Ground return**: fast nets over unbroken ground; checked by the review after routing.
