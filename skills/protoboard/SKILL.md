---
name: protoboard
description: Lay out a through-hole circuit on perfboard (protoboard) with the user, route it on the 2.54 mm hole grid and draw a build picture to copy by hand. Use when the user wants to build a circuit on perfboard, protoboard, prototype board or "dot board".
---

# Perfboard layouts with cadmate

Uses `list_protoboard_parts`, `check_protoboard`, `place_protoboard`, `route_protoboard` and `render_protoboard`. A design is a JSON dict (format in `src/cadmate/protoboard.py`'s docstring, example in `examples/protoboard/555-blinker.json`). Save it as a file next to the user's project and pass the path.

## What the tools assume

- Perfboard: separate pads. Parts go on top, through-hole only. Board `pads`: `single` (bottom only), `double` (both sides, plated through) or `double-unplated` (both sides, not joined). Ask which the user has; if they don't know, tell them to put a meter between the top and bottom pad of an empty hole. Plated boards route like single-sided ones (one node per hole); unplated ones get real top tracks, and via wires where a net changes side.
- Bottom wiring: solder bridges and bare tinned wire pad to pad along rows and columns. Two nets never share a pad, so bottom wires never cross. They may run under a part's body.
- Top wiring: insulated jumpers, straight, each end in its own free hole (never a lead's hole), never over a lead, a body or another jumper.
- Stripboard (Veroboard) and breadboard-pattern boards are not supported yet. If the user has one, say so before laying anything out: the routing is different (you cut strips instead of drawing wires).

## Steps

1. Get the circuit: a schematic, a netlist or the user's description. Write `nets` as `REF.PIN` lists. Check each part's pin numbering against `list_protoboard_parts` (DIP pin 1 bottom left with the notch to the left at rotation 0; diode and LED pin 1 = cathode; electrolytic pin 1 = +; TO-92 pin order is the datasheet's, which differs between parts).
2. Ask the user for the board size in holes if they have a board already. Leave a free column or row at the edges.
3. Place. Two ways, and mixing them is normal:
   - `place_protoboard`: the user's rough wishes become hints on the parts (`fixed` with `at`, `near`, `region`, `edge` for connectors, `keep_rotation`). It places a few times, routes each and keeps the best. Show the picture and ask what to move; turn the answer into hints and place again. Fix parts the user is happy with so they stay.
   - By hand, the way someone would on the bench:
     - Signal flow left to right, inputs on the left edge, outputs and connectors on an edge.
     - The IC in the middle with a free row above and below its pins; parts that join one IC pin go next to that pin.
     - Power and ground along two long runs (one row each, top and bottom of the board, or either side of the ICs), decoupling caps across the IC's supply pins.
     - Axial parts lying along the row they connect, not across other parts' pins.
     - A part's notes in `list_protoboard_parts` say when leads must be bent (tact switches, TO-92, small electrolytics); keep those parts where bending is easy.
4. `check_protoboard`. Fix every error. Info items are lead notes and unconnected pins: read them and tell the user about anything that matters.
5. `route_protoboard` with an `out_path`. It keeps any wires already in the design, so you or the user can draw the important ones (power runs, a ground bus) first under `wires` and let it do the rest.
6. `render_protoboard` and look at the picture before showing it. If `open` lists nets, or the routing is a tangle (long detours, many jumpers), move parts and route again rather than handing over a messy board. A few jumpers are normal; more than one per three nets usually means the placement can improve.
7. Give the user the PNG and the build list (`-build.md`). Point out that the bottom view is mirrored the way the board looks turned over, with the same row letters and column numbers, and that W1, W2... are insulated jumpers on the component side.

## Honest limits

- The router only uses straight runs along rows and columns and straight jumpers. It finishes small boards (a few ICs, up to roughly 30 parts) when the placement is sensible. On dense boards it lists what's still open: change the placement, or add the wire by hand.
- The automatic placer is good on small boards and loses to a careful hand on crowded ones. Placement is the part that makes a perfboard easy or awful to build: show it to the user before calling it done.
- It knows nothing about current or signal integrity: thick or long power runs, high-frequency or sensitive analog wiring still need judgement.
