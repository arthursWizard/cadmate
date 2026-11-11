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
]

MISMATCH = [
    ("0805", "R_0603_1608Metric", "the part is 0805"),
    ("SOT-23-5", "SOT-23-6", "5 pins but the footprint has 6"),
    ("SOT-23", "SOT-23-5", "3 pins"),
    ("QFN-32-EP(5x5)", "QFN-32-1EP_4x4mm_P0.4mm_EP2.7x2.7mm", "5.0x5.0 mm but the footprint's is 4.0x4.0"),
    ("TSSOP-20", "SOIC-20W_7.5x12.8mm_P1.27mm", "TSSOP but the footprint is SOIC"),
    ("HTSSOP-20", "TSSOP-20_4.4x6.5mm_P0.65mm", "HTSSOP"),  # its exposed pad has nowhere to go
    ("DO-214AC", "D_SMB", "SMA but the footprint is SMB"),
    ("SOD-323", "D_0805_2012Metric", "SOD-323 but the footprint is 0805"),
    ("SOT-23", "SOT-223-3_TabPin2", "SOT-223"),
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
    assert read_package("QFN-16-EP(3x3)")["body"] == (3.0, 3.0)
