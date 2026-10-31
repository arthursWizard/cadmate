from cadmate import fab_export as fx

BOARD = '''(kicad_pcb (version 20250926)
  (layers (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 10 10)
    (property "Reference" "R2") (property "Value" "10k") (property "LCSC" "C25744") (attr smd))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "B.Cu") (at 12 10)
    (property "Reference" "R10") (property "Value" "10k") (property "LCSC" "C25744") (attr smd))
  (footprint "Capacitor_SMD:C_0402_1005Metric" (layer "F.Cu") (at 14 10)
    (property "Reference" "C1") (property "Value" "100n") (attr smd))
  (footprint "cadmate:MountingHole_2.2mm_M2_SMDStandoff" (layer "B.Cu") (at 0 0)
    (property "Reference" "H1") (property "Value" "M2") (attr smd exclude_from_bom exclude_from_pos_files))
  (footprint "TestPoint:TestPoint_Pad_D1.0mm" (layer "B.Cu") (at 1 1)
    (property "Reference" "TP1") (property "Value" "SWCLK") (attr smd dnp))
)
'''

POS = """Ref,Val,Package,PosX,PosY,Rot,Side
"C1","100n","C_0402",114.0,-110.0,90.0,top
"R10","10k","R_0402",112.0,-110.0,180.0,bottom
"R2","10k","R_0402",110.0,-110.0,-90.0,top
"H1","M2","Hole",100.0,-100.0,0.0,bottom
"""


def test_bom_groups_by_value_footprint_and_part_and_lists_missing_numbers():
    parts = fx.footprints(BOARD)
    assert {p["ref"]: p["side"] for p in parts}["R10"] == "bottom"
    bom = fx.jlc_bom(parts).splitlines()
    assert bom[0] == "Comment,Designator,Footprint,LCSC Part #"
    assert bom[1:] == ['100n,C1,C_0402_1005Metric,', '10k,"R2,R10",R_0402_1005Metric,C25744']
    assert not any("H1" in l or "TP1" in l for l in bom)


def test_cpl_in_jlc_columns_without_holes_or_dnp():
    cpl = fx.jlc_cpl(POS, fx.footprints(BOARD)).splitlines()
    assert cpl[0] == "Designator,Mid X,Mid Y,Layer,Rotation"
    assert cpl[1:] == ["C1,114.0000mm,-110.0000mm,Top,90", "R2,110.0000mm,-110.0000mm,Top,270", "R10,112.0000mm,-110.0000mm,Bottom,180"]


def test_copper_layers_in_stack_order():
    assert fx.copper_layers(BOARD) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
