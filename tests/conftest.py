import os
import socket
import sys

import pytest

if os.environ.get("CADMATE_REQUIRE_FREECAD"):
    from cadmate import apps

    if apps.find_freecadcmd() is None:  # CI: a skip would hide a broken install
        raise RuntimeError("CADMATE_REQUIRE_FREECAD is set but freecadcmd wasn't found")


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch):
    """Tests never talk to a real KiCad, even when one is running with its API on."""
    from cadmate import kicad_pcb_live

    def refuse(*a, **kw):
        raise kicad_pcb_live.PcbUnavailable("tests don't reach KiCad")

    monkeypatch.setattr(kicad_pcb_live, "connect", refuse)


@pytest.fixture(autouse=True)
def bridge_already_offered(monkeypatch):
    """Tests don't get the once-per-session FreeCAD bridge check in tool replies (or ping a real bridge for it);
    the tests of that check turn it back on."""
    from cadmate import server

    monkeypatch.setattr(server, "_bridge_offered", True)


def short_tmp() -> str:
    """A fresh temp folder with a short path: macOS caps Unix socket paths at 104 bytes and pytest's tmp_path is longer.
    /tmp where there is one, the system temp folder elsewhere (Windows)."""
    import os
    import tempfile

    return tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)


# Tests that open a real Unix socket. Windows has no AF_UNIX in Python's socket module; the FreeCAD bridge uses a
# localhost port with a token there, and the TCP versions of these tests run on every OS.
unix_sockets = pytest.mark.skipif(sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
                                  reason="needs Unix sockets (Windows uses the bridge's TCP transport)")
