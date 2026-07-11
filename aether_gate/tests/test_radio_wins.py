#
# Aether-gate — "radio-state-wins on reconnect" tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""AE persists its last slice freq/mode and re-asserts it on reconnect via a
`slice create` carrying AE's remembered values — which would otherwise drag the
radio away from where it (or its front panel) actually is. On the FIRST slice
create after a (re)connect the engine overrides AE's values with the radio's
LIVE freq/mode (when the adapter can report them), so the rig stays put and AE
follows. Subsequent slice creates are honoured as AE asked.

These tests drive the real on_line('slice create ...') path with a stub conn
(captures the S/R lines) and a stub adapter reporting live radio state.

Run:  python -m aether_gate.tests.test_radio_wins
"""
import sys
import threading


class _Conn:
    """Minimal TCP-conn stand-in: captures everything the engine sends."""
    def __init__(self):
        self.out = bytearray()

    def sendall(self, b):
        self.out += b

    def text(self):
        return self.out.decode(errors="replace")


class _StubAdapter:
    """Reports a fixed live radio freq/mode; tune/set_mode are no-ops."""
    def __init__(self, freq_hz, mode):
        self._f = freq_hz
        self._m = mode
        self.capabilities = None
        self.tuned_to = []
        self.moded_to = []

    def radio_freq_hz(self):
        return self._f

    def radio_mode(self):
        return self._m

    # _sync_active_slice pushes the slice onto the radio via these:
    def set_slice(self, hz):
        self.tuned_to.append(hz)

    def set_mode(self, m):
        self.moded_to.append(m)


def _bare_radio(adapter):
    """A Radio with just the state on_line('slice create') touches — no socket,
    no stream threads, no real adapter open."""
    from aether_gate.core.engine import Radio
    r = Radio.__new__(Radio)
    r.adapter = adapter
    r.send_lock = threading.Lock()
    r.handle_hex = "0000AAAA"
    r.slices = {}
    r.pans = {0x40000000: {"center": 145.0, "slice": None, "wf_id": 0x42000000}}
    r.pan_seq = 0
    r.active_slice = 0
    r.slice_freq = 145.0
    r.slice_mode = "USB"
    r.max_slices = 8
    r._radio_state_claimed = False
    r._ae_drive_at = 0.0
    r.SUB_SLICE = 1
    r.conn = None
    r.center_mhz = 145.0
    r.span_mhz = 0.2
    r.min_dbm = -130
    r.max_dbm = -20
    # stub the pan/meter status emits (not under test here)
    r.emit_pan_status = lambda conn, pid=None: None
    r.emit_meter_status = lambda conn: None
    r._primary_pan = lambda: 0x40000000
    r._pan_from_kvs = lambda kvs: 0x40000000
    r._next_slice_index = lambda: (0 if 0 not in r.slices else
                                   next((i for i in range(r.max_slices) if i not in r.slices), None))
    return r


def _slice_create(r, conn, freq, mode, seq="1"):
    r.on_line(conn, f"C{seq}|slice create pan=0x40000000 freq={freq} antenna=ANT1 mode={mode}")


def test_first_slice_takes_radio_state_over_ae_memory():
    a = _StubAdapter(145_070_000, "FM")            # radio really on 145.07 FM
    r = _bare_radio(a)
    conn = _Conn()
    _slice_create(r, conn, "145.140100", "USB")    # AE re-asserts its stale 145.14 USB
    sl = r.slices[0]
    assert abs(sl["freq"] - 145.07) < 1e-6, sl["freq"]
    assert sl["mode"] == "FM", sl["mode"]
    # AE is told the radio's real state (so its display follows)
    assert "mode=FM" in conn.text(), conn.text()
    assert r._radio_state_claimed is True
    print("ok  radio-wins: first slice uses radio's 145.07 FM, not AE's 145.14 USB")


def test_second_slice_honours_ae_request():
    a = _StubAdapter(145_070_000, "FM")
    r = _bare_radio(a)
    _slice_create(r, _Conn(), "145.140100", "USB")  # first slice -> radio-wins
    # AE now deliberately creates a 2nd slice; that request must be honoured
    _slice_create(r, _Conn(), "144.390000", "USB", seq="2")
    sl1 = r.slices[1]
    assert abs(sl1["freq"] - 144.39) < 1e-6, sl1["freq"]
    assert sl1["mode"] == "USB", sl1["mode"]
    print("ok  radio-wins: 2nd slice honours AE's deliberate request")


def test_no_override_when_adapter_cannot_report():
    # An adapter without radio_freq_hz/radio_mode (e.g. IC-7300) keeps the old
    # behaviour: AE's requested slice values are used as-is.
    class _Dumb:
        capabilities = None
        def set_slice(self, hz): pass
        def set_mode(self, m): pass
    r = _bare_radio(_Dumb())
    _slice_create(r, _Conn(), "145.140100", "USB")
    sl = r.slices[0]
    assert abs(sl["freq"] - 145.1401) < 1e-6, sl["freq"]
    assert sl["mode"] == "USB", sl["mode"]
    print("ok  radio-wins: adapter without live readback -> AE's request kept")


def main():
    tests = [test_first_slice_takes_radio_state_over_ae_memory,
             test_second_slice_honours_ae_request,
             test_no_override_when_adapter_cannot_report]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} radio-wins tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
