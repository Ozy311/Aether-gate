#
# Aether-gate — the optional scope_center_hz() adapter hook (engine side).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
"""Where the engine frames a pan when the adapter says where its scope centre is.

An adapter may define scope_center_hz(slice_hz, mode). If it returns a number,
every place the engine frames a pan on a slice uses it; if the method is absent
or returns None, the engine does exactly what it did before. These tests drive
the gate's own control handler (Radio.on_line) with an adapter double and a
recording connection: no sockets, no radio.

Stdlib only. Run:  python3 -m aether_gate.tests.test_scope_center_hook
Exits non-zero on first failure.
"""
import sys

from aether_gate.adapters.base import AdapterCaps, RadioAdapter
from aether_gate.core import Radio

OFFSET_MHZ = 0.010


class _Conn:
    def __init__(self):
        self.lines = []

    def sendall(self, data):
        self.lines.extend(data.decode("utf-8").splitlines())

    def pan_lines(self):
        return [l for l in self.lines if "display pan 0x" in l]


class _Plain:
    """A native-centred adapter with NO hook (the shape of icom7300/hpsdr)."""
    provides = "spectrum"

    def __init__(self):
        self.capabilities = AdapterCaps(model="FLEX-6600", max_slices=1,
                                        native_centered_scope=True)
        self.slices = []

    def initial_center_hz(self):
        return 38_394_000

    def initial_mode(self):
        return "USB"

    def set_slice(self, hz):
        self.slices.append(hz)


class _Declines(_Plain):
    def scope_center_hz(self, slice_hz, mode):
        return None


class _Raises(_Plain):
    def scope_center_hz(self, slice_hz, mode):
        raise RuntimeError("adapter fault")


class _Inherits(RadioAdapter):
    """A real RadioAdapter that does not override the hook: base default None."""
    provides = "spectrum"
    capabilities = AdapterCaps(model="FLEX-6600", max_slices=1, native_centered_scope=True)

    def get_spectrum(self, ctx, t):
        return None


class _Offset(_Plain):
    """LO 10 kHz below the slice for upper-side modes, above it for lower."""

    def scope_center_hz(self, slice_hz, mode):
        side = +1 if (mode or "").upper() in ("LSB", "DIGL", "CWL") else -1
        return slice_hz + side * OFFSET_MHZ * 1e6


def _radio(adapter, slice_mhz=38.394, mode="USB", center=None):
    radio = Radio("127.0.0.1", None, adapter=adapter, port=5992)
    radio.streaming = True                         # no stream thread from emit_pan_status
    pid = radio._new_pan()
    radio.slices[0] = {"freq": slice_mhz, "mode": mode, "active": True, "pan": pid}
    radio.pans[pid]["slice"] = 0
    radio.pans[pid]["center"] = slice_mhz if center is None else center
    radio.active_slice = 0
    return radio, pid, _Conn()


def _close(a, b):
    return abs(a - b) < 1e-9


def test_no_hook_slice_tune_centres_on_slice():
    # Kills: consulting a hook that is not there, or changing the old framing.
    for cls in (_Plain, _Inherits, _Declines, _Raises):
        radio, pid, conn = _radio(cls(), center=38.400)
        radio.on_line(conn, "C1|slice tune 0 38.394000 autopan=0")
        assert radio.pans[pid]["center"] == 38.394, (cls.__name__, radio.pans[pid])
        assert "center=38.394000" in conn.pan_lines()[-1], conn.pan_lines()
    print("ok  scope hook: absent / None / raising -> pan centred on the slice (unchanged)")


def test_hook_moves_pan_centre_on_slice_tune():
    # Kills: the native_centered_scope branch ignoring the hook (the dead edge).
    radio, pid, conn = _radio(_Offset(), center=38.400)
    radio.on_line(conn, "C1|slice tune 0 38.394000 autopan=0")
    assert _close(radio.pans[pid]["center"], 38.384), radio.pans[pid]
    assert radio.slices[0]["freq"] == 38.394
    assert "center=38.384000" in conn.pan_lines()[-1], conn.pan_lines()
    print("ok  scope hook: slice tune centres the pan on the hook (LO), slice stays put")


def test_hook_mode_flip_recentres_and_reports():
    # Kills: comparing against the slice freq only (a mode-only change that
    # moves the LO to the other side would leave AE's pan on the old side).
    radio, pid, conn = _radio(_Offset(), center=38.384)
    radio.on_line(conn, "C1|slice set 0 mode=LSB")
    assert _close(radio.pans[pid]["center"], 38.404), radio.pans[pid]
    assert "center=38.404000" in conn.pan_lines()[-1], conn.pan_lines()
    print("ok  scope hook: a mode flip to the other side re-centres the pan and tells AE")


class _Jitter(_Offset):
    """A hook whose answer moves by a fraction of a Hz between calls, as one
    that quantises to a tuning word or rounds through MHz would."""

    def __init__(self):
        super().__init__()
        self.n = 0

    def scope_center_hz(self, slice_hz, mode):
        self.n += 1
        return super().scope_center_hz(slice_hz, mode) + (0.3 if self.n % 2 else -0.3)


def test_hook_same_tune_does_not_spam_pan_status():
    # Kills: an exact float compare on the hook path (sub-Hz differences in
    # the hook's answer would send a pan status line on every slice set).
    radio, pid, conn = _radio(_Jitter(), center=38.400)
    radio.on_line(conn, "C1|slice tune 0 38.394000 autopan=0")
    n = len(conn.pan_lines())
    for i in range(5):
        radio.on_line(conn, f"C{2 + i}|slice tune 0 38.394000 autopan=0")
    assert len(conn.pan_lines()) == n, conn.pan_lines()
    print("ok  scope hook: an unchanged tune sends no further pan status")


def test_hook_used_on_slice_create_and_autopan():
    # Kills: slice create (the first thing AE does on every connect) or the
    # autopan branch framing on the slice despite the hook.
    adapter = _Offset()
    radio = Radio("127.0.0.1", None, adapter=adapter, port=5992)
    radio.streaming = True
    radio._radio_state_claimed = True
    pid = radio._new_pan()
    conn = _Conn()
    radio.on_line(conn, f"C1|slice create pan=0x{pid:08X} freq=38.406000 mode=LSB")
    assert _close(radio.pans[pid]["center"], 38.416), radio.pans[pid]
    assert "center=38.416000" in conn.pan_lines()[-1], conn.pan_lines()
    radio.on_line(conn, "C2|slice set 0 RF_frequency=38.394000 mode=USB autopan=1")
    assert _close(radio.pans[pid]["center"], 38.384), radio.pans[pid]
    plain = Radio("127.0.0.1", None, adapter=_Plain(), port=5992)
    plain.streaming = True
    plain._radio_state_claimed = True
    ppid = plain._new_pan()
    plain.on_line(_Conn(), f"C1|slice create pan=0x{ppid:08X} freq=38.406000 mode=LSB")
    assert plain.pans[ppid]["center"] == 38.406, plain.pans[ppid]
    print("ok  scope hook: slice create and autopan use the hook; no hook -> slice")


def test_hook_pan_drag_keeps_pan_and_slice_offset():
    # AE drags the pan: it asks for centre X. The pan goes to X (never a
    # different centre, or AE's drag would fight the echo), and the slice keeps
    # the offset the hook gives, so the LO stays on the pan centre.
    # Kills: the old slice = pan centre on this path (Ozy's screenshot,
    # 2026-11-01: a pan drag left the slice on the centre and a dead edge).
    radio, pid, conn = _radio(_Offset(), center=38.384)
    for x in ("38.380000", "38.371396", "38.340971"):
        radio.on_line(conn, f"C1|display pan set 0x{pid:08X} center={x}")
        assert _close(radio.pans[pid]["center"], float(x)), radio.pans[pid]
        assert _close(radio.slices[0]["freq"], float(x) + OFFSET_MHZ), radio.slices[0]
        assert f"center={x}" in conn.pan_lines()[-1], conn.pan_lines()
    radio.slices[0]["mode"] = "LSB"
    radio.on_line(conn, f"C2|display pan set 0x{pid:08X} center=38.300000")
    assert _close(radio.slices[0]["freq"], 38.290), radio.slices[0]
    plain, ppid, pconn = _radio(_Plain())
    plain.on_line(pconn, f"C1|display pan set 0x{ppid:08X} center=38.340971")
    assert plain.slices[0]["freq"] == 38.340971 and plain.pans[ppid]["center"] == 38.340971
    print("ok  scope hook: pan drag -> pan at AE's centre, slice keeps its offset; no hook -> slice = centre")


def test_hook_centre_on_slice_request_never_walks_the_slice():
    # AE's centre lock sends `display pan set center=<slice freq>` after every
    # slice move. Mapping that like a drag would move the slice, AE would snap
    # again, and the slice would walk 10 kHz per round trip.
    # Kills: applying the drag mapping when AE asks to centre on the slice.
    radio, pid, conn = _radio(_Offset(), center=38.384)
    for i in range(10):
        radio.on_line(conn, f"C{i}|display pan set 0x{pid:08X} center=38.394000")
        assert radio.slices[0]["freq"] == 38.394, (i, radio.slices[0])
    assert radio.pans[pid]["center"] == 38.394
    print("ok  scope hook: centre-on-slice requests never move the slice (no runaway)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\n{len(tests)} scope-centre hook tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
