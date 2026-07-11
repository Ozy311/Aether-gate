#
# Aether-gate — IC-9700 mode-fold / echo tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""AE speaks Flex's mode vocabulary (USB/LSB/CW/AM/FM plus the data variants
DFM/DIGU/DIGL/...), which is a superset of the IC-9700's CI-V modes. These tests
pin two behaviours that together stop AE's mode from snapping back:

  1. _civ_mode_name folds every AE mode onto the CI-V base the 9700 can key
     (DFM->FM, DIGU->USB, DIGL->LSB, ...), or None when there's no equivalent —
     so a dropdown pick actually reaches the radio instead of being silently
     dropped (which let the radio->AE sync yank AE's choice straight back).

  2. _echo_mode reports the AE alias BACK to AE while the rig sits on the folded
     base mode (so DFM keeps showing DFM even though the 9700 is on plain FM),
     but yields to the rig's real mode once it diverges (a front-panel change),
     so those still reach AE.

Run:  python3 -m aether_gate.tests.test_ic9700_mode
"""
import sys


def test_base_modes_map_straight_through():
    from aether_gate.adapters.icom9700 import _civ_mode_name, MODE_TO_CIV
    for m in ("USB", "LSB", "FM", "AM", "CW", "RTTY", "DV"):
        assert _civ_mode_name(m) == m, (m, _civ_mode_name(m))
        assert m in MODE_TO_CIV
    # case-insensitive
    assert _civ_mode_name("fm") == "FM"
    print("ok  fold: base modes pass straight through (case-insensitive)")


def test_flex_data_variants_fold_to_base():
    from aether_gate.adapters.icom9700 import _civ_mode_name
    cases = {"DFM": "FM", "NFM": "FM", "DIGU": "USB", "DIGL": "LSB",
             "SAM": "AM", "CWU": "CW"}
    for ae, base in cases.items():
        assert _civ_mode_name(ae) == base, (ae, _civ_mode_name(ae))
    print("ok  fold: Flex data variants fold to their CI-V base")


def test_unmappable_modes_return_none():
    from aether_gate.adapters.icom9700 import _civ_mode_name
    for m in ("XYZ", "", None):
        assert _civ_mode_name(m) is None, (m, _civ_mode_name(m))
    print("ok  fold: genuinely-unknown modes return None (set_mode no-ops)")


def _bare_adapter():
    # An Icom9700Adapter with just the echo state — no sockets, no radio.
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    a = Icom9700Adapter.__new__(Icom9700Adapter)
    return a


def test_echo_keeps_ae_alias_while_rig_on_base():
    a = _bare_adapter()
    # AE asked for DFM -> we key FM on the rig, remember the alias.
    a._ae_mode_echo = "DFM"
    a._ae_mode_base = "FM"
    # Rig reads back FM (the base DFM folds to) -> report DFM so AE holds it.
    assert a._echo_mode("FM") == "DFM"
    print("ok  echo: DFM stays DFM while the rig sits on FM")


def test_echo_yields_to_frontpanel_change():
    a = _bare_adapter()
    a._ae_mode_echo = "DFM"
    a._ae_mode_base = "FM"
    # Someone turns the rig's MODE knob to USB: base no longer FM -> report the
    # rig's real mode so the front-panel change reaches AE.
    assert a._echo_mode("USB") == "USB"
    print("ok  echo: a front-panel change (base diverges) reaches AE verbatim")


def test_echo_passthrough_when_ae_never_set_mode():
    a = _bare_adapter()
    # No AE mode set yet (attrs absent): report the rig's mode as-is.
    assert a._echo_mode("USB") == "USB"
    assert a._echo_mode(None) is None
    print("ok  echo: no AE mode remembered -> rig mode passes through")


def main():
    tests = [test_base_modes_map_straight_through,
             test_flex_data_variants_fold_to_base,
             test_unmappable_modes_return_none,
             test_echo_keeps_ae_alias_while_rig_on_base,
             test_echo_yields_to_frontpanel_change,
             test_echo_passthrough_when_ae_never_set_mode]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} ic9700-mode tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
