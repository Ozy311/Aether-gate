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
    a._pan_ring.append((noise + dc).astype(np.complex64))
    with_dc = a._s_meter_dbm()
    a._pan_ring.clear()
    a._pan_ring.append(noise.astype(np.complex64))
    noise_only = a._s_meter_dbm()
    assert abs(with_dc - noise_only) < 1.0, (with_dc, noise_only)
    print("ok  flex1500: S-meter removes codec DC")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\n{len(tests)} flex1500 wire tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
