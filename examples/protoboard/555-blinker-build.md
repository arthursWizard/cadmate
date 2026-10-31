# 555 LED blinker

Perfboard, 20 columns x 14 rows, 2.54 mm pitch. Holes are row letter + column number, A1 at the top left seen from the component side. On the solder side A1 is at the top right.

## 1. Parts (top)

- **J1** 5V in (header 1x2, rotation 90): pins 1:B2, 2:C2
- **C3** 100n (cap_ceramic pitch 1, rotation 90): pins 1:B5, 2:C5
- **R1** 1k (resistor_axial span 4, rotation 0): pins 1:C6, 2:C10
- **R2** 10k (resistor_axial span 4, rotation 0): pins 1:C11, 2:C15
- **U1** NE555 (dip-8 0.3in, rotation 0): pins 1:H8, 2:H9, 3:H10, 4:H11, 5:E11, 6:E10, 7:E9, 8:E8
  - rotation 0: notch to the left, pin 1 bottom left, pins count counter-clockwise from the top
- **R3** 470 (resistor_axial span 4, rotation 0): pins 1:H14, 2:H18
- **C1** 10u (cap_electrolytic D6.3, rotation 0): pins 1:K10, 2:K11
  - pin 1 is +
- **C2** 10n (cap_ceramic pitch 1, rotation 0): pins 1:K14, 2:K15
- **D1** red (led 5mm, rotation 270): pins 1:L18, 2:K18
  - pin 1 is the cathode: short lead, flat side of the rim

## 2. Jumper wires (top, insulated)

- **W1** THR: J12 to F12 (4 holes)

## 3. Bottom wiring (solder side)

A run of one hole is a solder bridge between neighbouring pads; longer runs are bare tinned wire soldered at every pad.

- **CV**: E11 → E15 → J15 → J14 → K14 (wire over 12 pads)
- **DIS**: E9 → C9 → C10 (wire over 4 pads); C10 → C11 (bridge)
- **GND**: C2 → C5 (wire over 4 pads); C5 → H5 → H8 (wire over 9 pads); H8 → L8 → L11 → K11 (wire over 9 pads); L11 → L15 → K15 (wire over 6 pads); L15 → L18 (wire over 4 pads)
- **LED**: H18 → K18 (wire over 4 pads)
- **OUT**: H10 → I10 → I12 → H12 → H14 (wire over 7 pads)
- **THR**: H9 → K9 → K10 (wire over 5 pads); J9 → J12 (wire over 4 pads); F12 → F10 → E10 (wire over 4 pads); E10 → D10 → D15 → C15 (wire over 8 pads)
- **VCC**: B2 → B5 (wire over 4 pads); B5 → B6 → C6 (wire over 3 pads); C6 → C8 → E8 (wire over 5 pads); E8 → G8 → G11 → H11 (wire over 7 pads)

## Nets

- **CV**: U1.5, C2.1
- **DIS**: U1.7, R1.2, R2.1
- **GND**: J1.2, C3.2, U1.1, C1.2, C2.2, D1.1
- **LED**: R3.2, D1.2
- **OUT**: U1.3, R3.1
- **THR**: U1.6, U1.2, R2.2, C1.1
- **VCC**: J1.1, C3.1, U1.8, U1.4, R1.1
