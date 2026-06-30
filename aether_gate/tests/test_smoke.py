#
# Aether-gate — offline smoke tests (no hardware, no AE, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Gate for the scaffold: registry, both adapter paths, and the lifted VITA packers.

Run:  python -m aether_gate.tests.test_smoke   (or: python aether_gate/tests/test_smoke.py)
Exits non-zero on first failure.
"""
import math
import sys


def test_registry():
    from aether_gate.adapters import available, get_adapter, SimAdapter
    assert "sim" in available(), available()
    assert get_adapter("sim") is SimAdapter
    print("ok  registry: sim present")


def test_spectrum_adapter():
    from aether_gate.adapters import SimAdapter
    from aether_gate.core import PatternCtx
    a = SimAdapter(pattern="test_card")
    a.open()
    ctx = PatternCtx(1024, 512, -130.0, -20.0)
    levels = a.get_spectrum(ctx, 0.0)
    assert levels is not None and len(levels) == 1024, len(levels) if levels else None
    assert all(-130.0 - 1e-6 <= v <= -20.0 + 1e-6 for v in levels), "dBm out of display range"
    print("ok  spectrum adapter: 1024 bins in [min,max]")


def test_iq_path():
    from aether_gate.core.fft import iq_to_dbm
    n = 256
    # complex tone at +1/8 of the band -> peak should land right of centre after fftshift
    k = n // 8
    iq = [complex(math.cos(2 * math.pi * k * i / n), math.sin(2 * math.pi * k * i / n)) for i in range(n)]
    dbm = iq_to_dbm(iq, n, -130.0, 0.0)
    assert len(dbm) == n
    peak = max(range(n), key=lambda i: dbm[i])
    assert peak > n // 2, f"tone peak at bin {peak}, expected right of centre {n//2}"
    print(f"ok  iq path: tone peak at bin {peak} (>{n//2})")


def test_vita_packers():
    from aether_gate.core.engine import fft_packet, wf_packet, meter_packet
    pkt = fft_packet(0x40000000, 0, [100, 200, 300, 400], 7)
    assert len(pkt) >= 28 and len(pkt) % 4 == 0, len(pkt)        # 28B header + word-aligned payload
    wf = wf_packet(0x42000000, 0, [10, 20, 30, 40], 14_100_000, 244, 0, auto_black=70)
    assert len(wf) >= 28 and len(wf) % 2 == 0, len(wf)
    mt = meter_packet(0x46000000, 0, 10, -73.0)
    assert len(mt) == 28 + 4, len(mt)                            # header + (uint16 id + int16 raw)
    print("ok  vita packers: fft/wf/meter build")


def test_core_accepts_adapter():
    # Radio must construct with an adapter and pull its model from capabilities.
    from aether_gate.core import Radio
    from aether_gate.adapters import SimAdapter
    a = SimAdapter(pattern="carrier", model="FLEX-6700")
    r = Radio("127.0.0.1", None, adapter=a, port=5992)
    assert r.adapter is a
    assert r.model == "FLEX-6700", r.model
    assert r.max_slices == 8, r.max_slices                       # 6700 cap
    print("ok  core: Radio takes adapter, identity from caps")


def main():
    tests = [test_registry, test_spectrum_adapter, test_iq_path,
             test_vita_packers, test_core_accepts_adapter]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:                                   # import errors etc.
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
