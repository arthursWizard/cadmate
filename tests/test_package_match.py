import pytest

from cadmate.package_match import compare, read_footprint, read_package

MATCH = [
    ("0603", "R_0603_1608Metric"),
    ("0603 (1608 Metric)", "C_0603_1608Metric"),
    ("1608Metric", "R_0603_1608Metric"),
    ("C0805", "C_0805_2012Metric"),
    ("SOT-23", "SOT-23"),
    ("SOT-23-5", "SOT-23-5"),
    ("SOT23-6", "SOT-23-6"),
    ("SOT-25", "SOT-23-5"),
    ("SOT-363", "SOT-363_SC-70-6"),
    ("SC-70-6", "SOT-363_SC-70-6"),
    ("SOP-8", "SOIC-8_3.9x4.9mm_P1.27mm"),  # distributors' SOP-8 is the SOIC-8 body
    ("QFN-32-EP(5x5)", "QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"),
    ("VQFN-32", "QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"),
    ("LQFP-48(7x7)", "LQFP-48_7x7mm_P0.5mm"),
    ("SMA(DO-214AC)", "D_SMA"),
    ("DPAK", "TO-252-2"),
    ("HTSSOP-20", "HTSSOP-20-1EP_4.4x6.5mm_P0.65mm_EP3.4x6.5mm"),
    ("DIP-8", "DIP-8_W7.62mm"),
    # DigiKey: pin count first, width in words, alternatives
    ('8-SOIC (0.154", 3.90mm Width)', "SOIC-8_3.9x4.9mm_P1.27mm"),
    ("48-LQFP (7x7)", "LQFP-48_7x7mm_P0.5mm"),
    ('8-TSSOP, 8-MSOP (0.118", 3.00mm Width)', "MSOP-8_3x3mm_P0.65mm"),
    ("TO-252-3, DPak (2 Leads + Tab), SC-63", "TO-252-2"),  # the tab counted as a pin, or not
    # LCSC's long form and its other spellings
    ("QFN-32_L5.0-W5.0-P0.50-BL-EP3.5", "QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"),
    ("SOP-8_150mil", "SOIC-8_3.9x4.9mm_P1.27mm"),
    ("0603(1608)", "R_0603_1608Metric"),
    ("SMD,0603", "R_0603_1608Metric"),
    # exposed pads: KiCad's -1EP, the part's H/E prefix or EP
    ("HTSSOP-20", "TSSOP-20-1EP_4.4x6.5mm_P0.65mm_EP2.74x3.86mm"),
    ("ESOP-8", "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm"),
    ("SOIC-8", "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm"),  # a pad the part doesn't have is only a soldering question
    # other names for the same pads
    ("SC-74A", "SOT-23-5"), ("SOT-753", "SOT-23-5"), ("SC-74", "SOT-23-6"), ("TSOT-23-6", "SOT-23-6"),
    ("VFQFN-20", "QFN-20-1EP_4x4mm_P0.5mm_EP2.5x2.5mm"), ("UFQFPN-48", "QFN-48-1EP_7x7mm_P0.5mm_EP5.6x5.6mm"),
    ("QSOP-16", "SSOP-16_3.9x4.9mm_P0.635mm"),
    # KiCad names an SO body's width first, even when it's the long side
    ('8-TSSOP (0.173", 4.40mm Width)', "TSSOP-8_4.4x3mm_P0.65mm"),
    ("TSSOP-8_173mil", "TSSOP-8_4.4x3mm_P0.65mm"),
    ("SSOP-16_L4.9-W3.9-P0.64-LS6.0-BL", "SSOP-16_3.9x4.9mm_P0.635mm"),  # 0.64 is 0.635 rounded
    ("TDFN-8", "DFN-8-1EP_3x3mm_P0.65mm_EP1.55x2.4mm"),
]

MISMATCH = [
    ("0805", "R_0603_1608Metric", "the part is 0805"),
    ("SOT-23-5", "SOT-23-6", "5 pins but the footprint has 6"),
    ("SOT-23", "SOT-23-5", "3 pins"),
    ("QFN-32-EP(5x5)", "QFN-32-1EP_4x4mm_P0.4mm_EP2.7x2.7mm", "5x5 mm but the footprint's is 4x4"),
    ("TSSOP-20", "SOIC-20W_7.5x12.8mm_P1.27mm", "TSSOP but the footprint is SOIC"),
    ("HTSSOP-20", "TSSOP-20_4.4x6.5mm_P0.65mm", "exposed pad"),  # its pad underneath has nowhere to go
    ("DO-214AC", "D_SMB", "SMA but the footprint is SMB"),
    ("SOD-323", "D_0805_2012Metric", "SOD-323 but the footprint is 0805"),
    ("SOT-23", "SOT-223-3_TabPin2", "SOT-223"),
    ('14-SOIC (0.154", 3.90mm Width)', "SOIC-8_3.9x4.9mm_P1.27mm", "14 pins but the footprint has 8"),
    ("48-LQFP", "LQFP-64_10x10mm_P0.5mm", "48 pins"),
    ("8-WDFN", "QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm", "DFN but the footprint is QFN"),
    ('16-SOIC (0.154", 3.90mm Width)', "SOIC-16W_7.5x10.3mm_P1.27mm", "3.90 mm wide but the footprint's is 7.5"),
    ("SOP-8_208mil", "SOIC-8_3.9x4.9mm_P1.27mm", "5.28 mm wide"),
    ("QFN-32_L5.0-W5.0-P0.50-BL-EP3.5", "QFN-32-1EP_4x4mm_P0.4mm_EP2.7x2.7mm", "5x5 mm"),
    ("TO-263-7", "TO-263-2", "7 pins"),
    ("QSOP-16", "SSOP-16_5.3x6.2mm_P0.65mm", "pitch is 0.635 mm"),
    ("ESOP-8", "SOIC-8_3.9x4.9mm_P1.27mm", "exposed pad"),
    ("SMAF", "D_SMA", "SMAF but the footprint is SMA"),
    ('8-TSSOP (0.173", 4.40mm Width)', "TSSOP-8_3x3mm_P0.65mm", "4.40 mm wide but the footprint's is 3"),
    ("TO-220-3", "TO-220-2_Vertical", "3 pins"),  # TO-220-N counts real leads: no tab allowance
]


@pytest.mark.parametrize("package,footprint", MATCH)
def test_match(package, footprint):
    assert compare(package, footprint) == {"result": "match", "why": []}


@pytest.mark.parametrize("package,footprint,why", MISMATCH)
def test_mismatch(package, footprint, why):
    r = compare(package, footprint)
    assert r["result"] == "mismatch" and why in " ".join(r["why"]), r


def test_what_cant_be_read_is_unknown_not_a_guess():
    assert compare("Through Hole", "PinHeader_1x04_P2.54mm_Vertical")["result"] == "unknown"
    assert compare("SOT-23", "MyConnector_Custom")["result"] == "unknown"


def test_the_exposed_pad_size_isnt_the_body():
    assert read_footprint("QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm")["body"] == (5.0, 5.0)
    assert read_package("QFN-16-EP(3x3)")[0]["body"] == (3.0, 3.0)
