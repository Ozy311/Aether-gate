#
# Aether-gate — FLEX-1500 wire + RX-only guard tests (no hardware, no network).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
"""The FLEX-1500 wire facts and the RX-only send guard.

Stdlib only: runs in CI's no-pip job. Nothing here stands in for the radio's
firmware — the encoders are checked against values measured on a real
FLEX-1500 (fw 0.5.3.24, 2026-11-01), and the guard tests inject a recording
transport so they can assert that a refused frame produced NO write at all.

Run:  python3 -m aether_gate.tests.test_flex1500_wire
Exits non-zero on first failure.
"""
import os
import re
import sys

from aether_gate.adapters.flex1500 import wire as w


class _Recorder:
    """Transport stand-in: records every (endpoint, bytes) it is asked to write."""

    def __init__(self):
        self.writes = []

    def __call__(self, endpoint, data):
        self.writes.append((endpoint, bytes(data)))


def test_tuning_word_10mhz():
    # Kills: dropping the x2 (DDS runs at 2 x LO), using 400 MHz, or float floor.
    assert w.tuning_word(10_000_000) == 0x0D555555, hex(w.tuning_word(10_000_000))
    # 38.394 MHz: the LO for the live TCXO-spur proof.
    assert w.tuning_word(38_394_000) == (0xFFFFFFFF * 2 * 38_394_000) // 384_000_000
    # Quantised LO lands within one TW step (~0.045 Hz) of the request.
    assert abs(w.lo_hz_from_tw(w.tuning_word(14_074_000)) - 14_074_000) < 0.05
    print("ok  flex1500: tuning word 10 MHz = 0x0D555555")


def test_tuning_word_refuses_overflow():
    # Kills: removing the overflow check (the engine's tune envelope reaches 1.3 GHz).
    for bad in (192_000_001, 1_296_000_000):
        try:
            w.tuning_word(bad)
        except ValueError:
            continue
        raise AssertionError(f"tuning_word({bad}) did not refuse")
    print("ok  flex1500: tuning word refuses > 192 MHz")


def test_rx_coverage():
    # Kills: widening coverage past the datasheet's 490 kHz - 54 MHz.
    assert w.in_rx_coverage(490_000) and w.in_rx_coverage(54_000_000)
    assert not w.in_rx_coverage(489_999)
    assert not w.in_rx_coverage(54_000_001)
    assert not w.in_rx_coverage(145_000_000)
    print("ok  flex1500: RX coverage 490 kHz - 54 MHz")


def test_rx_filter_edges():
    # Kills: off-by-one at an edge (> vs >=) or a dropped/shifted edge.
    edges = w.RX_FILTER_EDGES_HZ
    assert len(edges) == 11
    assert w.rx_filter_index(100_000) == 0
    for i, e in enumerate(edges):
        assert w.rx_filter_index(e - 1) == i, (e, w.rx_filter_index(e - 1))
        assert w.rx_filter_index(e) == i + 1, (e, w.rx_filter_index(e))
    assert w.rx_filter_index(10_000_000) == 7      # 7.7 - 11.4 MHz
    assert w.rx_filter_index(38_400_000) == 11     # 37.6 - 54 MHz
    assert w.rx_filter_index(54_000_000) == 11
    print("ok  flex1500: RX filter index by edge")


def test_preamp_codes():
    # Kills: a shifted table (code 0 must be -10 dB).
    assert [w.preamp_code(d) for d in (-10, 0, 10, 20, 30)] == [0, 1, 2, 3, 4]
    try:
        w.preamp_code(15)
    except ValueError:
        pass
    else:
        raise AssertionError("preamp_code(15) accepted a non-step")
    print("ok  flex1500: preamp codes")


def test_command_frame_bytes():
    # Kills: little-endian params, wrong field offsets, wrong length.
    f = w.encode_command(w.OP_SET_RX1_FREQ_TW, 0x0D555555, 0, index=7)
    assert bytes(f) == bytes([7, 0, 0, 0,
                              0x00, 0x00, 0x05, 0x43,      # 1347 BE
                              0x0D, 0x55, 0x55, 0x55,      # p1 BE
                              0, 0, 0, 0, 0, 0, 0, 0]), bytes(f).hex()
    print("ok  flex1500: command frame layout")


def test_eeprom_read_frame():
    # Kills: wrong offset/count placement; a read encoder that could emit 1383.
    f = w.encode_eeprom_read(0x1800, 16)
    assert bytes(f[1:8]) == bytes([0x18, 0x00, 16, 0x00, 0x00, 0x05, 0x66]), bytes(f).hex()
    assert w.frame_opcode(f) == w.OP_READ_EEPROM == 1382
    assert not hasattr(w, "encode_eeprom_write")
    print("ok  flex1500: EEPROM read frame layout (no write encoder)")


def test_decode_reply():
    # Bytes as measured: GET_FIRMWARE_REV reply, status 0x39, type 1, index 1.
    r = w.decode_reply(bytes([0x39, 1, 1, 0, 0x00, 0x05, 0x03, 0x18]) + bytes(34))
    assert r[:4] == (0x39, 1, 1, 0x00050318), r
    assert w.fw_rev_ok(r[3]) and w.fw_rev_str(r[3]) == "0.5.3.24"
    assert not w.fw_rev_ok(0x00050317) and not w.fw_rev_ok(0x00060000)
    assert not w.fw_rev_ok(None)
    e = w.decode_reply(bytes([0x39, 2, 9, 0, 1, 2, 3]))
    assert e[1] == 2 and e[4] == bytes([1, 2, 3])
    assert w.decode_reply(b"\x39\x00") is None
    st = w.decode_status(0x39)
    assert not any(st.values()), st                 # idle: nothing pressed
    assert w.decode_status(0x38)["mic_ptt"] is True  # PTT pressed clears bit 0
    print("ok  flex1500: reply + status decode")


def test_iq_handedness_conjugates():
    # Kills: dropping the Q negation (a carrier above the LO would land below it).
    pkt = bytes([0x00, 0x40, 0x00, 0x20])            # I=0x4000, Q=0x2000
    (s,) = w.iq_from_packet(pkt)
    assert s == complex(0.5, -0.25), s
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        a = w.iq_from_packet(pkt * 3, np)
        assert a.dtype == np.complex64 and len(a) == 3
        assert complex(a[0]) == complex(0.5, -0.25), a[0]
    print("ok  flex1500: I/Q conjugated (Q negated)")


def test_allow_and_forbid_are_disjoint():
    # Kills: a forbidden opcode slipping onto the allowlist.
    assert not (w.ALLOWED_OPCODES & set(w.FORBIDDEN_OPCODES)), \
        w.ALLOWED_OPCODES & set(w.FORBIDDEN_OPCODES)
    for op in (1276, 1292, 1383, 1219, 1019, 1020, 1021, 1304, 1305,
               1269, 1298, 1322, 1325, 1279, 1260):
        assert op in w.FORBIDDEN_OPCODES, op
    print("ok  flex1500: allowlist and forbid list disjoint")


def test_forbidden_opcodes_never_write():
    # Kills: removing the check in send_frame (or moving it after the write).
    rec = _Recorder()
    for op in w.FORBIDDEN_OPCODES:
        try:
            w.send_frame(rec, w.encode_command(op, 1, 0), 1)
        except w.RefusedOpcode:
            continue
        raise AssertionError(f"opcode {op} was not refused")
    assert rec.writes == [], rec.writes
    print("ok  flex1500: every forbidden opcode refused with zero writes")


def test_unlisted_opcode_never_writes():
    # Kills: replacing the allowlist with a denylist-only check.
    rec = _Recorder()
    for op in (0, 1287, 1266, 1278, 1256, 9999):     # FPLED, READ_PTT, RX ant, XREF, junk
        try:
            w.send_frame(rec, w.encode_command(op), 1)
        except w.RefusedOpcode:
            continue
        raise AssertionError(f"unlisted opcode {op} was not refused")
    assert rec.writes == [], rec.writes
    print("ok  flex1500: unlisted opcodes refused with zero writes")


def test_malformed_frames_never_write():
    # Kills: trusting the caller's frame shape (a 1383 smuggled in a 1382 frame,
    # junk in the padding, or a short/long frame).
    rec = _Recorder()
    bad = []
    f = w.encode_command(w.OP_GET_STATUS); f[2] = 1; bad.append(f)        # padding 1..3
    f = w.encode_command(w.OP_GET_STATUS); f[18] = 1; bad.append(f)       # padding 16..19
    f = w.encode_eeprom_read(0x1800, 8); f[4:8] = (1383).to_bytes(4, "big"); bad.append(f)
    f = w.encode_eeprom_read(0x1800, 8); f[3] = 33; bad.append(f)         # count > 32
    bad.append(bytearray(19))
    bad.append(w.encode_command(w.OP_GET_STATUS) + b"\x00")
    for f in bad:
        try:
            w.send_frame(rec, f, 1)
        except w.RefusedOpcode:
            continue
        raise AssertionError(f"malformed frame {bytes(f).hex()} was not refused")
    assert rec.writes == [], rec.writes
    print("ok  flex1500: malformed frames refused with zero writes")


def test_channel_refusal_and_endpoint():
    # Kills: CommandChannel bypassing send_frame, or writing to any EP but 0x04.
    rec = _Recorder()
    ch = w.CommandChannel(rec)
    try:
        ch.write_op(1276, 1)                          # SET_TR(1) = key the radio
    except w.RefusedOpcode:
        pass
    else:
        raise AssertionError("CommandChannel sent SET_TR")
    assert rec.writes == [] and ch.refused == 1 and ch.writes_ok == 0
    ch.write_op(w.OP_SET_TRX_PREAMP, 1)
    assert rec.writes == [(w.EP_CMD_OUT, bytes(w.encode_command(1247, 1, 0, index=1)))], rec.writes
    assert all(ep == 0x04 for ep, _ in rec.writes)
    print("ok  flex1500: channel refuses SET_TR; allowed ops go only to EP 0x04")


def test_channel_reply_matching_and_index_wrap():
    # The gate's own request/reply bookkeeping, driven directly (no radio model):
    # a reply for another index must not satisfy a request; index skips 0.
    rec = _Recorder()
    ch = w.CommandChannel(rec)
    ch.on_interrupt_in(bytes([0x39, 1, 2, 0, 0, 0, 0, 9]))     # stale reply, index 2
    assert ch.read_op(w.OP_GET_FIRMWARE_REV, timeout=0.05) is None   # sent as index 1
    assert ch.last_status == 0x39
    seen = set()
    for _ in range(300):
        ch.write_op(w.OP_GET_STATUS)
    for ep, data in rec.writes:
        seen.add(data[0])
    assert 0 not in seen and len(seen) == 255, (0 in seen, len(seen))
    print("ok  flex1500: replies index-matched; index wraps 1..255")


_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "adapters", "flex1500")


def test_source_has_one_write_path_and_no_iso_out():
    # §I as an enforced invariant, not a convention: exactly one interrupt write
    # in the package (inside the adapter's transport wrapper), no bulk/control
    # writes, every iso transfer set up on the IN endpoint, no key_tx.
    # Kills: adding a second write path, an iso-OUT submit, or a key_tx().
    srcs = {}
    for name in sorted(os.listdir(_PKG)):
        if name.endswith(".py"):
            with open(os.path.join(_PKG, name), encoding="utf-8") as fh:
                srcs[name] = fh.read()
    code = {n: "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
            for n, s in srcs.items()}
    allcode = "\n".join(code.values())
    assert len(re.findall(r"\.interruptWrite\(", allcode)) == 1, "more than one EP write path"
    assert not re.search(r"\.(bulkWrite|controlWrite)\(", allcode)
    iso = re.findall(r"\.setIsochronous\(\s*([^,]+),", allcode)
    assert iso and all(a.strip() == "wire.EP_ISO_IN" for a in iso), iso
    assert "EP_ISO_OUT_TX" not in code["adapter.py"]
    assert not re.search(r"def\s+(key_tx|arm_tx)\b", allcode)
    print("ok  flex1500: one EP write path, iso IN only, no key_tx")


def test_adapter_declares_rx_only():
    # Kills: tx_capable flipped on, or wants_tx overridden to assert TX.
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    from aether_gate.adapters.base import RadioAdapter
    a = Flex1500Adapter()
    assert a.capabilities.tx_capable is False
    assert type(a).wants_tx is RadioAdapter.wants_tx and a.wants_tx(None) is None
    assert not hasattr(a, "key_tx")
    assert a.capabilities.max_span_hz == a.capabilities.min_span_hz == w.SAMPLE_RATE
    assert a.current_span_hz() == w.SAMPLE_RATE
    assert "2m" not in a.capabilities.bands and "6m" in a.capabilities.bands
    print("ok  flex1500: adapter is RX-only, 48 kHz span, HF+6m bands")


def test_adapter_refuses_out_of_coverage_retune_without_write():
    # §II: a refused tune must not read as a successful one, and must not write.
    # Kills: dropping the coverage check in _apply_tune.
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    a = Flex1500Adapter(center_hz=14_100_000.0)
    rec = _Recorder()
    a._chan = w.CommandChannel(rec)
    assert a._apply_tune(145_000_000.0) is False
    assert rec.writes == [] and a.center_hz == 14_100_000.0 and a.tune_refused == 1
    assert a._apply_tune(38_394_000.0) is True
    ops = [w.frame_opcode(d) for _, d in rec.writes]
    assert ops == [w.OP_SET_RX1_FILTER, w.OP_SET_RX1_FREQ_TW], ops
    assert a.center_hz == 38_394_000.0 and a.filter_index == 11
    print("ok  flex1500: out-of-coverage retune refused, no write, LO kept")


def test_adapter_failed_write_keeps_previous_lo():
    # §II: a failed command must not read as a successful one.
    # Kills: updating center_hz before (or regardless of) the TW write.
    from aether_gate.adapters.flex1500 import Flex1500Adapter

    class _Failing(_Recorder):
        def __call__(self, endpoint, data):
            if w.frame_opcode(data) == w.OP_SET_RX1_FREQ_TW:
                raise OSError("simulated USB write failure")
            super().__call__(endpoint, data)

    a = Flex1500Adapter(center_hz=14_100_000.0)
    a._chan = w.CommandChannel(_Failing())
    assert a._apply_tune(7_074_000.0) is False
    assert a.center_hz == 14_100_000.0 and a.tune_failed == 1
    print("ok  flex1500: failed TW write keeps the previous LO")


def test_adapter_diagnostics_before_open():
    # The control panel may poll before or after open(); neither diagnostics()
    # nor read_meters() may raise, and the no-read-back caveat must be there (§II).
    # Kills: a broken import or attribute on the diagnostics path (found live:
    # a wrong relative import took /diagnostics down with a 500).
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    a = Flex1500Adapter()
    d = a.diagnostics()
    assert d["radio"] == "FLEX-1500" and "no read-back" in d["commanded_note"], d
    assert d["counters"]["cmd_refused"] == 0 and d["status_byte"] is None
    assert a.read_meters().s_meter_dbm == -140.0 and a.read_meters().noise_dbm is None
    print("ok  flex1500: diagnostics/read_meters safe before open")


def test_smeter_ignores_codec_dc():
    # Measured with no antenna: codec DC (~-147/+55 counts) sits ~45 dB above
    # the ~1 LSB noise. The derived S-meter must report the noise, not the DC.
    # Kills: dropping the block-mean removal in _s_meter_dbm.
    try:
        import numpy as np
    except ImportError:
        print("skip flex1500: S-meter DC test needs numpy")
        return
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    a = Flex1500Adapter()
    a._np = np
    rng = np.random.default_rng(1)
    n = 4096
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / w.FULL_SCALE
    dc = complex(-147, -55) / w.FULL_SCALE        # conjugated Q, as the adapter stores it
    a._pan_ring.append((None, 0, (noise + dc).astype(np.complex64)))   # (lo, idx, iq)
    with_dc = a._s_meter_dbm()
    a._pan_ring.clear()
    a._pan_ring.append((None, 0, noise.astype(np.complex64)))
    noise_only = a._s_meter_dbm()
    assert abs(with_dc - noise_only) < 1.0, (with_dc, noise_only)
    print("ok  flex1500: S-meter removes codec DC")


def test_adapter_asks_once_per_slice_and_centre():
    # The engine calls set_slice() every audio frame and get_iq() every pan
    # frame. A refused tune must be requested once, not every frame.
    # Kills: comparing to the hardware LO instead of the last request
    # (found live: 1402 refusals in ~30 s for a slice on 98.68 MHz).
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    a = Flex1500Adapter(center_hz=14_100_000.0)
    a.set_slice(98_682_540.0)
    assert a._retune_to == 98_682_540.0
    a._retune_to = None
    for _ in range(5):
        a.set_slice(98_682_540.0)
    assert a._retune_to is None, a._retune_to
    a._seeded = True
    a._ae_center_hz = 98_697_900.0
    for _ in range(5):
        a.get_iq(1024, 98_697_900.0, 48_000.0)
    assert a._retune_to is None
    for _ in range(5):                             # set_mode/get_audio repeat every frame
        a.set_mode("USB")
    assert a._retune_to is None
    # A new centre is ONE request. With the default 10 kHz IF offset and USB the
    # LO goes 10 kHz below the centre (it was the centre itself before offset
    # tuning, which put the slice on the DC spike).
    a.get_iq(1024, 7_074_000.0, 48_000.0)
    assert a._retune_to == 7_064_000.0, a._retune_to
    a._retune_to = None
    for _ in range(5):
        a.get_iq(1024, 7_074_000.0, 48_000.0)
    assert a._retune_to is None
    b = Flex1500Adapter(center_hz=14_100_000.0, if_offset_hz=0)   # offset off = legacy
    b._seeded = True
    b._ae_center_hz = 14_100_000.0
    b.get_iq(1024, 7_074_000.0, 48_000.0)
    assert b._retune_to == 7_074_000.0, b._retune_to
    print("ok  flex1500: one retune request per slice/centre change")


def test_adapter_relaxes_once_after_drag_despite_per_frame_calls():
    # The engine calls get_iq() every pan frame with the same centre. Those calls
    # must not count as "the slice moved", or the relax timer never expires and
    # the unsampled edge stays wide after every drag; and relax asks only once.
    # Kills: dropping the input dedupe in _evaluate_lo, or relax never wired.
    import time as _t
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    a = Flex1500Adapter(center_hz=14_074_000.0)
    a._seeded = True
    a._ae_center_hz = 14_074_000.0
    a._lo_req = 14_064_000.0
    a.set_slice(14_074_000.0)
    a.get_iq(1024, 14_079_000.0, 48_000.0)         # drag 5 kHz: hysteresis keeps the LO
    a.set_slice(14_079_000.0)
    assert a._retune_to is None and a._lo_req == 14_064_000.0
    t0 = a._lo_changed_at
    for _ in range(50):                            # per-frame calls, same inputs
        a.get_iq(1024, 14_079_000.0, 48_000.0)
        a.set_slice(14_079_000.0)
        a.set_mode("USB")
    assert a._lo_changed_at == t0, "per-frame calls reset the relax timer"
    a._maybe_relax(t0 + 1.0)
    assert a._retune_to is None                    # not yet
    a._maybe_relax(t0 + 2.5)
    assert a._retune_to == 14_069_000.0, a._retune_to
    a._retune_to = None
    a._maybe_relax(t0 + 10.0)
    assert a._retune_to is None                    # once only
    print("ok  flex1500: relax fires once after a drag, per-frame calls ignored")


def test_adapter_slice_jump_is_one_retune_to_the_preferred_lo():
    # The engine moves AE's pan centre onto the slice before it calls
    # set_slice(); the LO decision must use that centre, not the previous one,
    # or a 12 kHz jump picks an LO 2 kHz under the slice (fine for the OLD pan),
    # keeps it, and needs a second retune later.
    # Kills: deciding set_slice() against the stale pan centre.
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    a = Flex1500Adapter(center_hz=38_394_000.0)
    a._seeded = True
    a._ae_center_hz = 38_394_000.0
    a._lo_req = 38_384_000.0
    a.set_slice(38_394_000.0)
    a._retune_to, n0 = None, a.retunes_requested
    a.set_slice(38_406_000.0)                      # engine order: set_slice, then get_iq
    a.get_iq(1024, 38_406_000.0, 48_000.0)
    assert a._retune_to == 38_396_000.0, a._retune_to
    assert a.retunes_requested - n0 == 1, a.retunes_requested - n0
    print("ok  flex1500: a slice jump is one retune, straight to the preferred LO")


# --- offset tuning: wire.choose_lo (stdlib) ------------------------------------
_V = 14_074_000.0


def test_lo_offset_side_per_mode():
    # LO below the slice for the upper family, above for the lower family, below
    # for AM/FM; the slice (pan centre, native-centred) is never on the LO.
    # Kills: side flipped for LSB, or no offset applied (LO == centre).
    for mode, want in (("USB", _V - 10_000), ("DIGU", _V - 10_000), ("CW", _V - 10_000),
                       ("LSB", _V + 10_000), ("DIGL", _V + 10_000), ("CWL", _V + 10_000),
                       ("AM", _V - 10_000), ("FM", _V - 10_000), ("", _V - 10_000)):
        got = w.choose_lo(_V, _V, mode, None, 10_000)
        assert got == want, (mode, got, want)
        assert w.lo_clearance(got, _V, mode) >= w.LO_GUARD_HZ, mode
    print("ok  flex1500: LO side per mode (below for USB/CW/AM/FM, above for LSB)")


def test_lo_offset_zero_is_legacy():
    # --flex1500-if-offset 0 must reproduce LO = pan centre exactly.
    # Kills: an offset applied when the switch says off.
    assert w.choose_lo(_V, _V, "USB", None, 0) == _V
    assert w.choose_lo(_V, _V, "LSB", _V, 0) is None
    assert w.choose_lo(_V + 5.0, _V, "USB", _V, 0) == _V + 5.0
    # an LO that the offset policy would keep is still moved back to the centre
    assert w.choose_lo(_V, _V, "USB", _V - 5_000.0, 0) == _V
    print("ok  flex1500: IF offset 0 = LO on the pan centre (legacy)")


def _drag(start, stop, step, mode, offset=10_000):
    """Drag a native-centred slice (centre == slice) in `step` Hz increments,
    applying every LO the policy asks for. Returns (requests, final_lo, min_clear)."""
    lo = w.choose_lo(start, start, mode, None, offset)
    n, f, worst = 0, start, 1e9
    while (stop - f) * step > 0:
        f += step
        new = w.choose_lo(f, f, mode, lo, offset)
        if new is not None:
            lo, n = new, n + 1
        worst = min(worst, w.lo_clearance(lo, f, mode))
        assert abs(f - lo) <= offset + w.LO_DEAD_EXTRA_HZ, (f, lo)
    return n, lo, worst


def test_lo_hysteresis_on_drag():
    # A slice drag must not retune every step (each retune drops 150 ms of IQ),
    # and the passband must never come within the guard of the LO while it moves.
    # Kills: hysteresis removed (keep-current check dropped -> one request per
    # step), or the guard check dropped (passband reaches the LO).
    n_up, _, worst_up = _drag(_V, _V + 50_000, 10.0, "USB")
    n_dn, _, worst_dn = _drag(_V, _V - 50_000, -10.0, "USB")
    n_lsb, _, worst_lsb = _drag(_V, _V + 50_000, 10.0, "LSB")
    # 5000 steps each way; 50 kHz / ~6-8.5 kHz of travel per LO = <= 9 retunes
    assert 1 <= n_up <= 9 and 1 <= n_dn <= 9 and 1 <= n_lsb <= 9, (n_up, n_dn, n_lsb)
    assert min(worst_up, worst_dn, worst_lsb) >= w.LO_GUARD_HZ, (worst_up, worst_dn, worst_lsb)
    # the first 5 kHz of an upward drag costs no retune at all
    lo = w.choose_lo(_V, _V, "USB", None, 10_000)
    for k in range(1, 501):
        assert w.choose_lo(_V + 10 * k, _V + 10 * k, "USB", lo, 10_000) is None, k
    print(f"ok  flex1500: drag 50 kHz -> {n_up}/{n_dn}/{n_lsb} LO moves, guard held")


def test_lo_relax_returns_to_preferred():
    # After a drag the kept LO may be 15 kHz off; relax asks for the preferred one.
    # Kills: relax ignored (unsampled edge stays wide after every drag).
    lo = _V - 10_000
    f = _V + 5_000
    assert w.choose_lo(f, f, "USB", lo, 10_000) is None
    assert w.choose_lo(f, f, "USB", lo, 10_000, relax=True) == f - 10_000
    assert w.choose_lo(f, f, "USB", f - 10_000, 10_000, relax=True) is None
    print("ok  flex1500: relax returns the LO to its preferred offset")


def test_lo_coverage_edges_no_storm():
    # At the coverage edges the preferred side can fall outside 0.49-54 MHz:
    # flip to the other side, and once applied ask nothing more. Out of coverage
    # entirely: ask once for the centre (so _apply_tune refuses it and says so),
    # then nothing. Kills: coverage ignored in lo_ok (requests an LO the radio
    # cannot take), or a re-request on every evaluation (the 1402x storm).
    lo = w.choose_lo(495_000.0, 495_000.0, "USB", None, 10_000)
    assert lo == 505_000.0 and w.in_rx_coverage(lo), lo
    assert w.choose_lo(495_000.0, 495_000.0, "USB", lo, 10_000) is None
    hi = w.choose_lo(53_995_000.0, 53_995_000.0, "LSB", None, 10_000)
    assert hi == 53_985_000.0 and w.in_rx_coverage(hi), hi
    assert w.choose_lo(53_995_000.0, 53_995_000.0, "LSB", hi, 10_000) is None
    far = 98_682_540.0
    assert w.choose_lo(far, far, "USB", 14_064_000.0, 10_000) == far
    assert w.choose_lo(far, far, "USB", far, 10_000) is None
    print("ok  flex1500: coverage edges flip side; out of coverage asks once")


def test_if_offset_validation():
    # Kills: accepting an offset that leaves no guard (1 kHz) or a pan mostly unsampled.
    assert w.check_if_offset(0) == 0 and w.check_if_offset(10_000) == 10_000
    for bad in (1_000, 20_000, -5_000):
        try:
            w.check_if_offset(bad)
        except ValueError:
            continue
        raise AssertionError(f"IF offset {bad} accepted")
    from aether_gate.adapters.flex1500 import Flex1500Adapter
    try:
        Flex1500Adapter(if_offset_hz=500)
    except ValueError:
        pass
    else:
        raise AssertionError("adapter accepted a 500 Hz IF offset")
    print("ok  flex1500: IF offset validated (0 or 2-16 kHz)")


def test_cli_switches_reach_the_adapter():
    # Kills: the flags parsed but not passed through (A/B switch silently inert).
    from aether_gate import __main__ as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "--flex1500-no-dc-block" in src and "--flex1500-if-offset" in src
    assert "dc_block=not args.flex1500_no_dc_block" in src
    assert "if_offset_hz=args.flex1500_if_offset" in src
    print("ok  flex1500: --flex1500-no-dc-block / --flex1500-if-offset wired")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\n{len(tests)} flex1500 wire tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
