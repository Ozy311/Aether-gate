#
# Aether-gate — IC-9700 power read tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""RF power on the 9700 is two DIFFERENT things:
  * the SETTING  — CI-V 14 0A, a 0..100 % level (AE's 'RF Power' slider), and
  * measured FORWARD power — CI-V 15 11 Po meter, non-linear 0..255 -> watts.
These tests pin both reads + the Po curve conversion.

Run:  python -m aether_gate.tests.test_ic9700_power
"""
import sys


class _Civ:
    def __init__(self, rfpower_raw=None, fwdpwr_raw=None, freq_hz=145_070_000):
        self.rfpower_raw = rfpower_raw
        self.fwdpwr_raw = fwdpwr_raw
        self.freq_hz = freq_hz


def _adapter(**civ_kw):
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    a = Icom9700Adapter.__new__(Icom9700Adapter)
    a._civ = _Civ(**civ_kw)
    return a


def test_power_level_is_percent_not_watts():
    # 14 0A: raw 3 -> 1% (matches Nigel's rig at 1% setting), 255 -> 100%.
    assert _adapter(rfpower_raw=3).radio_power_level() == 1
    assert _adapter(rfpower_raw=255).radio_power_level() == 100
    assert _adapter(rfpower_raw=128).radio_power_level() == 50
    assert _adapter(rfpower_raw=None).radio_power_level() is None
    print("ok  power: 14 0A reports a 0..100 percent LEVEL (raw3->1%)")


def test_fwd_power_curve_2m():
    # 15 11 Po meter -> watts, non-linear, band max 100 W on 2m.
    a = lambda raw: _adapter(fwdpwr_raw=raw, freq_hz=145_070_000)._fwd_power_w()
    assert a(0) == 0.0
    assert abs(a(143) - 50.0) < 0.5      # calibration midpoint
    assert abs(a(213) - 100.0) < 0.5     # full scale
    assert a(255) == 100.0               # saturates
    # low end where Nigel measured ~0.78 W: should be a few watts at most, small
    assert 0.0 <= a(10) <= 5.0, a(10)
    print("ok  fwd: 15 11 Po curve -> watts on 2m (0/50/100 at raw 0/143/213)")


def test_fwd_power_23cm_max_10w():
    # 23cm rated 10 W: full-scale Po = 10 W, not 100.
    a = _adapter(fwdpwr_raw=213, freq_hz=1_296_000_000)
    assert abs(a._fwd_power_w() - 10.0) < 0.2, a._fwd_power_w()
    print("ok  fwd: 23cm forward power scales to 10 W max")


def test_fwd_power_none_without_reading():
    assert _adapter(fwdpwr_raw=None)._fwd_power_w() is None
    print("ok  fwd: no 15 11 reading -> None (RX / not yet polled)")


def main():
    tests = [test_power_level_is_percent_not_watts, test_fwd_power_curve_2m,
             test_fwd_power_23cm_max_10w, test_fwd_power_none_without_reading]
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
    print(f"\nall {len(tests)} power tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
