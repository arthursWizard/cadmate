import socket
import sys

import pytest


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch):
    """Tests never talk to a real KiCad, even when one is running with its API on."""
    from cadmate import kicad_pcb_live

    def refuse(*a, **kw):
        raise kicad_pcb_live.PcbUnavailable("tests don't reach KiCad")

    monkeypatch.setattr(kicad_pcb_live, "connect", refuse)


def short_tmp() -> str:
    """A fresh temp folder with a short path: macOS caps Unix socket paths at 104 bytes and pytest's tmp_path is longer.
    /tmp where there is one, the system temp folder elsewhere (Windows)."""
    import os
    import tempfile

    return tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)


# Tests that open a real Unix socket. Windows has no AF_UNIX in Python's socket module, and cadmate's FreeCAD bridge
# says so instead of connecting.
unix_sockets = pytest.mark.skipif(sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
                                  reason="needs Unix sockets (the FreeCAD bridge isn't supported on Windows)")
