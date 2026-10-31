import shutil
from pathlib import Path

import pytest

from cadmate import kicad_pcb_live as pcb, live
from conftest import short_tmp

BOARD = '''(kicad_pcb
	(version 20250926)
	(generator "pcbnew")
	(net 0 "")
	(net 1 "GND")
	(net 2 "VCC")
	(footprint "Capacitor_SMD:C_0603_1608Metric"
		(layer "F.Cu")
		(uuid "fp-c1")
		(at 10 20 90)
		(property "Reference" "C1" (at 0 -1.4 0) (layer "F.SilkS") (uuid "p1"))
		(property "Value" "100n" (at 0 1.4 0) (layer "F.Fab") (uuid "p2"))
		(pad "1" smd roundrect (at -0.8 0 90) (size 0.9 0.95) (layers "F.Cu" "F.Mask" "F.Paste") (net 1 "GND") (uuid "pd1"))
	)
	(footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
		(layer "B.Cu")
		(uuid "fp-u1")
		(at 30 20 0)
		(property "Reference" "U1" (at 0 -3.4 0) (layer "B.SilkS") (uuid "p3"))
		(property "Value" "NE555" (at 0 3.4 0) (layer "B.Fab") (uuid "p4"))
	)
	(segment (start 10 19.2) (end 20 19.2) (width 0.25) (layer "F.Cu") (net 1) (uuid "t1"))
	(via (at 20 19.2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
	(gr_rect (start 0 0) (end 50 40) (stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts") (uuid "g1"))
)
'''


@pytest.fixture(autouse=True)
def data(monkeypatch):
    d = Path(short_tmp())
    monkeypatch.setenv(live.SOCKET_ENV, str(d / "b.sock"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Doc:
    board_filename = "test.kicad_pcb"


class FakeBoard:
    """Stands in for kipy's Board: the board text is what get_as_string returns."""

    def __init__(self, text):
        self.text = text
        self.commits = []
        self.document = Doc()
        self.name = "test.kicad_pcb"
        self.open = None

    def get_as_string(self):
        return self.text

    def begin_commit(self):
        self.open = self.text
        return object()

    def push_commit(self, commit, message=""):
        self.commits.append(message)
        self.open = None

    def drop_commit(self, commit):
        self.text = self.open
        self.open = None

    def get_selection(self):
        return []


class FakeKiCad:
    def __init__(self, board):
        self.board = board
        self.actions = []

    def run_action(self, action):
        self.actions.append(action)
        if action == pcb.UNDO_ACTION:
            self.board.redo_text, self.board.text = self.board.text, self.board.undo_text
        else:
            self.board.text = self.board.redo_text


@pytest.fixture
def board(monkeypatch):
    b = FakeBoard(BOARD)
    k = FakeKiCad(b)
    monkeypatch.setattr(pcb, "connect", lambda *a, **kw: (k, b))
    monkeypatch.setattr(pcb, "_namespace", lambda kicad, board: {"board": board, "result": None})
    return b


def test_index_reads_footprints_tracks_and_nets():
    idx = pcb.index(BOARD)
    assert idx["fp-c1"]["summary"] == {"type": "footprint", "ref": "C1", "value": "100n", "lib": "Capacitor_SMD:C_0603_1608Metric",
                                       "x": 10.0, "y": 20.0, "rotation": 90.0, "side": "front", "locked": False}
    assert idx["fp-u1"]["summary"]["side"] == "back"
    assert idx["t1"]["summary"] == {"type": "track", "start": [10.0, 19.2], "end": [20.0, 19.2], "width": 0.25, "layer": "F.Cu", "net": "GND"}
    assert idx["v1"]["summary"]["net"] == "GND" and idx["g1"]["summary"]["type"] == "rect"


def test_kicad10_net_names():
    text = BOARD.replace('(net 1)', '(net "GND")')
    assert pcb.index(text)["t1"]["summary"]["net"] == "GND"


def test_diff_moves_adds_removes_and_inner_changes():
    a = pcb.index(BOARD)
    text = BOARD.replace("(at 10 20 90)", "(at 12 20 90)").replace('(via (at 20 19.2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))\n', "")
    text = text.replace('(property "Value" "NE555" (at 0 3.4 0)', '(property "Value" "NE555" (at 0 4 0)')
    text = text.replace("\t(gr_rect", '\t(segment (start 20 19.2) (end 25 19.2) (width 0.25) (layer "B.Cu") (net 1) (uuid "t2"))\n\t(gr_rect')
    d = pcb.diff(a, pcb.index(text))
    assert d["changed"] and d["count"] == 4
    by = {i["item"] + ("+" if i.get("added") else "-" if i.get("removed") else ""): i for i in d["items"]}
    assert by["footprint C1"]["changed"] == {"x": [10.0, 12.0]}
    assert "note" in by["footprint U1"]
    assert by["via on GND-"]["removed"] and by["track on GND+"]["now"]["layer"] == "B.Cu"


def test_whitespace_only_is_not_a_change():
    a = pcb.index(BOARD)
    assert not pcb.diff(a, pcb.index(BOARD.replace("(at 10 20 90)", "(at  10 20 90)")))["changed"]


def test_edit_reports_user_changes_then_its_own(board):
    assert pcb.state()["user_changes"] == {"first_look": True}
    board.text = BOARD.replace("(at 30 20 0)", "(at 31 20 0)")  # the user moves U1
    code = "board.text = board.text.replace('(at 10 20 90)', '(at 10 22 90)')"
    r = pcb.edit(code, "C1 2 mm down")
    assert r["ok"], r
    assert r["user_changes"]["items"] == [{"item": "footprint U1", "changed": {"x": [30.0, 31.0]}}]
    assert r["changed"]["items"] == [{"item": "footprint C1", "changed": {"y": [20.0, 22.0]}}]
    assert board.commits == ["cadmate: C1 2 mm down"]
    assert pcb.state()["user_changes"]["changed"] is False  # cadmate's own edit isn't the user's
    assert pcb.state()["history"] == ["C1 2 mm down"]


def test_failed_edit_drops_the_commit(board):
    r = pcb.edit("board.text = board.text.replace('C1', 'C9')\nraise ValueError('nope')", "x")
    assert not r["ok"] and "nope" in r["error"] and board.text == BOARD and board.commits == []


def test_undo_only_when_nothing_happened_since(board):
    pcb.state()
    board.undo_text = BOARD
    pcb.edit("board.text = board.text.replace('(at 10 20 90)', '(at 10 22 90)')", "C1 down")
    board.text = board.text.replace("(at 30 20 0)", "(at 31 20 0)")
    r = pcb.undo()
    assert not r["ok"] and "changed after" in r["error"]

    board.text = BOARD
    pcb.state()
    pcb.edit("board.text = board.text.replace('(at 10 20 90)', '(at 10 22 90)')", "C1 down again")
    board.undo_text = BOARD
    r = pcb.undo()
    assert r["ok"] and r["undid"] == "C1 down again" and board.text == BOARD


def test_no_kicad_says_how_to_enable(monkeypatch):
    from cadmate import server

    def refuse(*a, **kw):
        raise pcb.PcbUnavailable("can't reach KiCad's API")

    monkeypatch.setattr(pcb, "connect", refuse)
    r = server.kicad_pcb_state()
    assert not r["ok"] and "Enable KiCad API" in r["how"]


def test_undo_refused_after_a_user_change_cadmate_already_reported(board):
    pcb.state()
    board.undo_text = BOARD
    pcb.edit("board.text = board.text.replace('(at 10 20 90)', '(at 10 22 90)')", "C1 down")
    board.text = board.text.replace("(at 30 20 0)", "(at 31 20 0)")
    assert pcb.state()["user_changes"]["changed"]  # reported, and so seen
    r = pcb.undo()
    assert not r["ok"] and "changed after" in r["error"]
    assert "(at 31 20 0)" in board.text and "(at 10 22 90)" in board.text


def test_undo_that_takes_back_something_else_is_redone(board):
    pcb.state()
    pcb.edit("board.text = board.text.replace('(at 10 20 90)', '(at 10 22 90)')", "C1 down")
    board.undo_text = board.text.replace("(at 30 20 0)", "(at 35 20 0)")  # KiCad's undo list holds something else on top
    edited = board.text
    r = pcb.undo()
    assert not r["ok"] and "redid" in r["error"] and board.text == edited
    assert pcb.state()["history"] == ["C1 down"]


def test_connect_falls_back_to_a_standalone_pcb_editor(monkeypatch):
    """The project manager answers on the default socket without a board; a PCB editor opened on its own has api-<pid>.sock."""
    import kipy

    from cadmate import kicad_pcb_live

    class Fake:
        def __init__(self, socket_path=None, timeout_ms=0):
            self.sock = socket_path

        def ping(self):
            pass

        def get_board(self):
            if self.sock is None:
                raise RuntimeError("no handler available for GetOpenDocuments")
            return "board on " + self.sock

    monkeypatch.undo()  # conftest's refusal of connect
    monkeypatch.setattr(kipy, "KiCad", Fake)
    monkeypatch.setattr(kicad_pcb_live, "_other_sockets", lambda: ["ipc:///tmp/kicad/api-123.sock"])
    kicad, board = kicad_pcb_live.connect()
    assert board == "board on ipc:///tmp/kicad/api-123.sock"
    monkeypatch.setattr(kicad_pcb_live, "_other_sockets", lambda: [])
    with pytest.raises(kicad_pcb_live.PcbUnavailable, match="no board open"):
        kicad_pcb_live.connect()
