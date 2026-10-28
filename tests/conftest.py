import pytest


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch):
    """Tests never talk to a real KiCad, even when one is running with its API on."""
    from cadmate import kicad_pcb_live

    def refuse(*a, **kw):
        raise kicad_pcb_live.PcbUnavailable("tests don't reach KiCad")

    monkeypatch.setattr(kicad_pcb_live, "connect", refuse)
