#
# Aether-gate — IC-9700 LAN RX-audio parse tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Pin the RS-BA1 audio-packet parse + the 48k->24k decimation get_audio uses.

Run:  python -m aether_gate.tests.test_icom_audio
"""
import struct
import sys


def _audio_pkt(pcm_bytes, seq=1):
    """Build a 0x18-header RS-BA1 audio datagram carrying pcm_bytes (int16 LE)."""
    hdr = bytearray(0x18)
    struct.pack_into("<I", hdr, 0x00, 0x18 + len(pcm_bytes))  # len
    struct.pack_into("<H", hdr, 0x04, 0x0000)                 # type
    struct.pack_into("<H", hdr, 0x06, seq)                    # seq
    struct.pack_into(">H", hdr, 0x16, len(pcm_bytes))         # datalen (BE)
    return bytes(hdr) + pcm_bytes


def _fresh_audio():
    from aether_gate.adapters.icom.audio import Ic9700Audio
    a = Ic9700Audio.__new__(Ic9700Audio)      # skip socket adoption
    import threading
    a._ring = bytearray()
    a._ring_lock = threading.Lock()
    a.audio_frames = 0
    a.audio_bytes = 0
    a.dropped = 0
    return a


def test_parse_and_read_samples():
    a = _fresh_audio()
    # 4 int16 samples: 0, 16384(=0.5), -16384(-0.5), 32767(~1.0)
    pcm = struct.pack("<4h", 0, 16384, -16384, 32767)
    a._on_audio(_audio_pkt(pcm))
    assert a.audio_frames == 1
    assert a.ring_samples == 4, a.ring_samples
    out = a.read_samples(4)
    assert len(out) == 4
    assert abs(out[0] - 0.0) < 1e-6
    assert abs(out[1] - 0.5) < 1e-3
    assert abs(out[2] + 0.5) < 1e-3
    assert out[3] > 0.99
    assert a.ring_samples == 0                # drained
    print("ok  audio: 0x18 header parsed, LPCM16 -> float, ring drains")


def test_short_read_returns_available():
    a = _fresh_audio()
    a._on_audio(_audio_pkt(struct.pack("<2h", 100, 200)))
    out = a.read_samples(10)                   # ask more than we have
    assert len(out) == 2, out
    assert a.read_samples(1) == []             # empty now
    print("ok  audio: short ring returns what's there, then empty")


def test_odd_payload_trimmed():
    a = _fresh_audio()
    # 5 bytes = 2 whole int16 + 1 stray byte -> stray dropped
    a._on_audio(_audio_pkt(b"\x01\x00\x02\x00\x7f"))
    assert a.ring_samples == 2, a.ring_samples
    print("ok  audio: odd trailing byte trimmed to whole samples")


def test_get_audio_decimates_48k_to_24k():
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    ad = Icom9700Adapter.__new__(Icom9700Adapter)
    ad._audio = _fresh_audio()
    # feed 256 samples @48k -> get_audio(128) should return 128 @24k (every 2nd)
    pcm = struct.pack("<256h", *[(i % 100) * 100 for i in range(256)])
    ad._audio._on_audio(_audio_pkt(pcm))
    got = ad.get_audio(128)
    assert got is not None and len(got) == 128, (got is None, len(got) if got else 0)
    # decimated: got[0]==src[0], got[1]==src[2] ...
    assert abs(got[0] - 0.0) < 1e-6
    assert abs(got[1] - (200 / 32768.0)) < 1e-4     # src[2] = (2%100)*100 = 200
    print("ok  audio: get_audio decimates 48k->24k, returns 128 samples")


def test_get_audio_none_without_stream():
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    ad = Icom9700Adapter.__new__(Icom9700Adapter)
    ad._audio = None
    assert ad.get_audio(128) is None
    print("ok  audio: get_audio None when no audio session")


def test_dispatch_subclass_routes_audio_to_ring():
    # REGRESSION: the SDR9700 transport port left Ic9700Audio (a bare UdpBase)
    # with NO _dispatch_subclass, so arriving audio datagrams were counted but
    # never handed to on_data -> the ring stayed empty -> AE got pure silence
    # even with a loud signal on the wire. Pin that a full audio datagram fed to
    # _dispatch_subclass reaches _on_audio and fills the ring.
    import time
    a = _fresh_audio()
    a.on_data = a._on_audio
    a._mono_start = time.monotonic()
    a._last_received_ms = 0
    pcm = struct.pack("<4h", 0, 16384, -16384, 32767)
    a._dispatch_subclass(_audio_pkt(pcm))
    assert a.audio_frames == 1, a.audio_frames
    assert a.ring_samples == 4, a.ring_samples
    print("ok  audio: _dispatch_subclass routes a data datagram to the ring")


def test_dispatch_subclass_ignores_control_frames():
    # A 0x10 control packet must NOT be parsed as audio (no ring growth).
    import time
    a = _fresh_audio()
    a.on_data = a._on_audio
    a._mono_start = time.monotonic()
    a._last_received_ms = 0
    a._areyouthere_on = True
    a.remote_id = 0
    ctrl = struct.pack("<IHHII", 0x10, 0x04, 0, 0, 0)   # len16, type 0x04 "I am here"
    a._dispatch_subclass(ctrl)
    assert a.ring_samples == 0
    assert a._areyouthere_on is False        # 0x04 stops the are-you-there timer
    print("ok  audio: _dispatch_subclass ignores control frames, handles 0x04")


# ---- TX audio (Stage 2: gate -> radio) ------------------------------------

class _FakeSock:
    """Captures datagrams instead of sending; enough for send_tracked/_write."""
    def __init__(self):
        self.sent = []
    def sendto(self, data, addr):
        self.sent.append(bytes(data)); return len(data)
    def getsockname(self):
        return ("127.0.0.1", 40411)


def _tx_audio():
    """An Ic9700Audio wired just enough to exercise send_audio (no real socket)."""
    from aether_gate.adapters.icom.audio import Ic9700Audio
    import threading
    a = Ic9700Audio.__new__(Ic9700Audio)
    a.sock = _FakeSock()
    a.my_id = 0x11111111
    a.remote_id = 0x22222222
    a.send_seq = 1
    a._send_audio_seq = 0
    a.tx_frames = 0
    a.tx_bytes = 0
    a._lock = threading.Lock()
    a._tx_seq_buf = {}
    a._idle_on = False
    a.n_sent = 0
    a.packets_sent = 0
    a._mono_start = None
    # send_tracked calls _elapsed_ms + _purge_old_entries; stub them cheap.
    a._elapsed_ms = lambda: 0
    a._purge_old_entries = lambda: None
    a._write = lambda buf: a.sock.sendto(buf, (a.radio_ip if hasattr(a, "radio_ip") else "127.0.0.1", 0))
    return a


def test_send_audio_frames_one_packet():
    from aether_gate.adapters.icom.audio import AUDIO_HDR
    a = _tx_audio()
    pcm = struct.pack("<4h", 1, 2, 3, 4)        # 8 bytes, one fragment
    a.send_audio(pcm)
    assert len(a.sock.sent) == 1, len(a.sock.sent)
    d = a.sock.sent[0]
    assert struct.unpack_from("<I", d, 0)[0] == AUDIO_HDR + 8      # len (LE)
    assert struct.unpack_from("<H", d, 0x10)[0] == 0x0080          # ident TX-audio
    assert struct.unpack_from(">H", d, 0x12)[0] == 0              # sendseq (BE), first=0
    assert struct.unpack_from(">H", d, 0x16)[0] == 8              # datalen (BE)
    assert struct.unpack_from("<I", d, 0x08)[0] == 0x11111111     # sentid
    assert struct.unpack_from("<I", d, 0x0c)[0] == 0x22222222     # rcvdid
    assert d[AUDIO_HDR:] == pcm                                    # payload intact
    assert a.tx_frames == 1 and a.tx_bytes == 8
    print("ok  tx: send_audio frames a single packet (ident 0x0080, BE seq/datalen)")


def test_send_audio_fragments_and_increments_seq():
    from aether_gate.adapters.icom.audio import _TX_FRAG, AUDIO_HDR
    a = _tx_audio()
    pcm = b"\x01\x02" * (_TX_FRAG)              # 2*_TX_FRAG bytes -> 2 fragments
    a.send_audio(pcm)
    assert len(a.sock.sent) == 2, len(a.sock.sent)
    # fragment sizes: first == _TX_FRAG, second == remainder
    assert struct.unpack_from(">H", a.sock.sent[0], 0x16)[0] == _TX_FRAG
    assert struct.unpack_from(">H", a.sock.sent[1], 0x16)[0] == _TX_FRAG
    # audio sendseq increments per fragment (BE)
    assert struct.unpack_from(">H", a.sock.sent[0], 0x12)[0] == 0
    assert struct.unpack_from(">H", a.sock.sent[1], 0x12)[0] == 1
    # transport seq (stamped by send_tracked at [6:8], LE) also increments
    assert struct.unpack_from("<H", a.sock.sent[0], 6)[0] == 1
    assert struct.unpack_from("<H", a.sock.sent[1], 6)[0] == 2
    print("ok  tx: send_audio fragments at 1364B, sendseq + transport-seq increment")


def test_send_audio_empty_noop():
    a = _tx_audio()
    a.send_audio(b"")
    assert a.sock.sent == []
    print("ok  tx: send_audio('') is a no-op")


def test_engine_drain_tx_audio():
    # drain_tx_audio pops mono int16 from tx_pcm_ring (whole samples), empties.
    from aether_gate.core.engine import Radio
    import threading
    r = Radio.__new__(Radio)
    r.tx_pcm_ring = bytearray(struct.pack("<4h", 10, 20, 30, 40))
    r.tx_ring_lock = threading.Lock()
    first = r.drain_tx_audio(4)                 # 4 bytes = 2 samples
    assert first == struct.pack("<2h", 10, 20), first
    rest = r.drain_tx_audio()                   # all remaining
    assert rest == struct.pack("<2h", 30, 40), rest
    assert r.drain_tx_audio() == b""            # empty
    print("ok  tx: engine.drain_tx_audio pops whole samples, then empties")


def test_upsample_2x_doubles_and_interpolates():
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    ad = Icom9700Adapter.__new__(Icom9700Adapter)
    ad._tx_resample_carry = b""
    # two samples 0, 100 -> expect [0, 50] emitted (last sample carried)
    out = ad._upsample_2x(struct.pack("<2h", 0, 100))
    vals = struct.unpack(f"<{len(out)//2}h", out)
    assert vals == (0, 50), vals
    # next chunk continues from the carried 100: sample 200 -> [100, 150]
    out2 = ad._upsample_2x(struct.pack("<1h", 200))
    vals2 = struct.unpack(f"<{len(out2)//2}h", out2)
    assert vals2 == (100, 150), vals2
    print("ok  tx: _upsample_2x doubles rate, interpolates across chunk seam")


def main():
    tests = [test_parse_and_read_samples, test_short_read_returns_available,
             test_odd_payload_trimmed, test_get_audio_decimates_48k_to_24k,
             test_get_audio_none_without_stream,
             test_dispatch_subclass_routes_audio_to_ring,
             test_dispatch_subclass_ignores_control_frames,
             test_send_audio_frames_one_packet,
             test_send_audio_fragments_and_increments_seq,
             test_send_audio_empty_noop,
             test_engine_drain_tx_audio,
             test_upsample_2x_doubles_and_interpolates]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} audio tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
