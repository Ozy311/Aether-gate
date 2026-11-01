#
# Aether-gate — FLEX-1500 wire primitives (pure functions + the one EP4 write path).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See LICENSE.
#
"""FLEX-1500 USB wire facts, kept stdlib-only so they test without hardware.

Clean-room: every value here comes from the FLEX-1500 service manual / datasheet
or from our own read-only and RX-only probes of a real radio (firmware 0.5.3.24,
measured 2026-11-01, no antenna). Nothing is copied from another FLEX-1500
host implementation.

Device shape (measured): USB 2192:1502, full speed, one configuration, all four
endpoints on interface 3, no alternate settings, no kernel driver bound:

  0x82  iso IN    192 B / 1 ms = 48 frames of int16-LE interleaved I,Q @ 48 kHz.
                  Streams as soon as iso transfers are submitted (no start opcode).
  0x83  int IN    status byte + command replies. The radio speaks only on a
                  change or a reply, so read timeouts here are NORMAL.
  0x04  int OUT   20-byte command frames.
  0x01  iso OUT   carries TX I/Q when the radio is keyed. THIS MODULE AND THE
                  ADAPTER NEVER SUBMIT ANYTHING TO IT.

SAFETY — the whole point of this file. send_frame() is the ONLY function in the
flex1500 package that writes to the radio. It refuses, BEFORE any USB write:
  * every opcode on FORBIDDEN_OPCODES (T/R = MOX, EEPROM write, initialise,
    raw I2C / FlexWire, keying / amplifier sequencing, TX antenna, PA filter);
  * every opcode not on ALLOWED_OPCODES (read-only queries plus the three
    volatile RX setters: RX1 filter, preamp, RX1 tuning word);
  * any frame whose encoded opcode, length or padding does not match.
The RX setters are volatile — the radio's SPI bus is outbound only and a power
cycle restores it — so nothing this adapter can send persists in the radio.
"""
import struct
import threading

# --- device identity / endpoints (measured, lsusb -v 2026-11-01) -------------
VID = 0x2192
PID = 0x1502
INTERFACE = 3
EP_ISO_IN = 0x82
EP_INT_IN = 0x83
EP_CMD_OUT = 0x04
EP_ISO_OUT_TX = 0x01           # named only so tests can assert it is never used

ISO_PACKET_BYTES = 192         # 48 frames x (I int16 + Q int16)
SAMPLE_RATE = 48_000           # one rate only; 192 B x 1000 packets/s
FULL_SCALE = 32768.0           # int16 -> [-1, 1)
CMD_FRAME_BYTES = 20
INT_IN_BYTES = 42              # wMaxPacketSize of 0x83 (lsusb)

# DDS: AD9951 clocked at 384 MHz (38.4 MHz TCXO x 10) and run at 2 x LO into a
# divide-by-2 quadrature Johnson counter (service manual pp. 8-9). Hence the
# factor 2 in the tuning word. The external-reference 400 MHz mode is not used.
DDS_CLOCK_HZ = 384_000_000

# RX coverage per the FLEX-1500 datasheet: 490 kHz - 54 MHz. Outside it the
# adapter keeps the previous LO and says so; it does not dial an out-of-range
# word (above 192 MHz the 32-bit tuning word would overflow outright).
RX_MIN_HZ = 490_000
RX_MAX_HZ = 54_000_000

# Firmware gate: the accepted range for this radio family. The test radio
# reports 0x00050318 = 0.5.3.24 (matches bcdDevice 0.05). Anything else -> the
# adapter refuses to open rather than guess at a different firmware's protocol.
FW_MIN = 0x00050318
FW_MAX = 0x0005FFFF

# --- opcodes ------------------------------------------------------------------
OP_GET_FIRMWARE_REV = 1200
OP_GET_TRX_SN = 1203
OP_GET_SERIAL_NUM = 1220
OP_GET_REGION = 1350
OP_GET_STATUS = 1355
OP_READ_EEPROM = 1382
OP_SET_RX1_FILTER = 1257
OP_SET_TRX_PREAMP = 1247
OP_SET_RX1_FREQ_TW = 1347

ALLOWED_OPCODES = frozenset({
    OP_GET_FIRMWARE_REV, OP_GET_TRX_SN, OP_GET_SERIAL_NUM, OP_GET_REGION,
    OP_GET_STATUS, OP_READ_EEPROM,                           # read-only
    OP_SET_RX1_FILTER, OP_SET_TRX_PREAMP, OP_SET_RX1_FREQ_TW,  # volatile RX setters
})

# Refused by name even though the allowlist would already refuse them: if
# someone widens ALLOWED_OPCODES by mistake, these still never reach the radio.
FORBIDDEN_OPCODES = {
    1276: "SET_TR (this is MOX: keys the transmitter)",
    1292: "SET_MOX",
    1383: "WRITE_EEPROM (firmware and calibration share the one EEPROM)",
    1219: "INITIALIZE",
    1019: "I2C_WRITE_VALUE (raw internal I2C)",
    1020: "I2C_WRITE_2_VALUE (raw internal I2C)",
    1021: "I2C_READ_VALUE (raw internal I2C)",
    1304: "FlexWire raw I2C",
    1305: "FlexWire raw I2C",
    1269: "SET_RCA_TX1 (rear PTT-out / amplifier keying)",
    1298: "SET_AMP_TX1 (TX-out sequencing)",
    1322: "SET_AMP_TX1 delay enable",
    1325: "SET_AMP_TX1 delay",
    1279: "SET_TX_ANT",
    1260: "SET_PA_FILTER",
}

# RX band-pass filter edges, MHz (service manual p. 8): index 0 is bypass below
# 0.48 MHz, index 11 is 37.6-54 MHz.
RX_FILTER_EDGES_HZ = (480_000, 880_000, 1_600_000, 2_300_000, 3_500_000,
                      5_200_000, 7_700_000, 11_400_000, 17_000_000,
                      25_300_000, 37_600_000)

# SET_TRX_PREAMP codes 0..4 -> nominal gain, dB. Nominal: with no antenna the
# effect on the noise floor was NOT characterised (DC offsets and noise moved at
# every step, but not monotonically with gain).
PREAMP_DB = (-10, 0, 10, 20, 30)

# Interrupt-IN byte 0: live status bits, ACTIVE-LOW (bit clear = pressed).
# Idle measured 0x39. The adapter only REPORTS these; it never acts on them.
STATUS_MIC_PTT = 0x01
STATUS_REAR_PTT = 0x08
STATUS_DASH = 0x10
STATUS_DOT = 0x20

REPLY_U32 = 1                  # bytes 4..7 = big-endian u32 result
REPLY_EEPROM = 2               # data from byte 4


class RefusedOpcode(Exception):
    """Raised before any USB write when a frame is not allowed to reach the radio."""


# --- pure helpers --------------------------------------------------------------
def tuning_word(hz):
    """32-bit AD9951 tuning word for an RX LO of `hz`.

    TW = floor((2^32 - 1) * 2 * f / 384 MHz). Integer arithmetic on whole Hz:
    float floor can land one count off. 10.000 MHz -> 0x0D555555 (verified live
    on the TCXO spur, which tracked LO steps exactly).
    """
    f = int(round(hz))
    if f < 0:
        raise ValueError(f"negative frequency {hz!r}")
    tw = (0xFFFFFFFF * 2 * f) // DDS_CLOCK_HZ
    if tw > 0xFFFFFFFF:
        raise ValueError(f"{hz!r} Hz overflows the 32-bit tuning word")
    return tw


def lo_hz_from_tw(tw):
    """The LO the radio actually runs for tuning word `tw` (quantised, ~0.045 Hz steps)."""
    return tw * DDS_CLOCK_HZ / (2.0 * 0xFFFFFFFF)


def in_rx_coverage(hz):
    return RX_MIN_HZ <= hz <= RX_MAX_HZ


def rx_filter_index(hz):
    """SET_RX1_FILTER index (0..11) for an LO of `hz`: number of edges at or below it."""
    i = 0
    while i < len(RX_FILTER_EDGES_HZ) and hz >= RX_FILTER_EDGES_HZ[i]:
        i += 1
    return i


def preamp_code(db):
    """SET_TRX_PREAMP code for a nominal gain in dB. ValueError if not a step."""
    db = int(db)
    if db not in PREAMP_DB:
        raise ValueError(f"preamp {db} dB is not one of {PREAMP_DB}")
    return PREAMP_DB.index(db)


def fw_rev_ok(v):
    return v is not None and FW_MIN <= v <= FW_MAX


def fw_rev_str(v):
    return f"{(v >> 24) & 255}.{(v >> 16) & 255}.{(v >> 8) & 255}.{v & 255}"


def decode_status(b):
    """Active-low status byte -> which inputs read as pressed."""
    return {"mic_ptt": not (b & STATUS_MIC_PTT),
            "rear_ptt": not (b & STATUS_REAR_PTT),
            "dash": not (b & STATUS_DASH),
            "dot": not (b & STATUS_DOT)}


# --- frames --------------------------------------------------------------------
def encode_command(op, p1=0, p2=0, index=0):
    """20-byte command frame: [0]=index, [1..3]=0, [4..7]=op, [8..11]=p1,
    [12..15]=p2, [16..19]=0, all big-endian. Encoding only — sending goes
    through send_frame(), which is where the allowlist is enforced."""
    return bytearray(struct.pack(">B3xIII4x", index & 0xFF, op, p1 & 0xFFFFFFFF,
                                 p2 & 0xFFFFFFFF))


def encode_eeprom_read(offset, count, index=0):
    """READ_EEPROM frame: [0]=index, [1..2]=offset BE, [3]=count, [4..7]=1382.
    Read only; there is deliberately no write encoder in this package."""
    if not 0 <= offset <= 0x3FFF:
        raise ValueError(f"EEPROM offset 0x{offset:x} outside 16 KB")
    if not 1 <= count <= 32:
        raise ValueError(f"EEPROM read count {count} outside 1..32")
    f = bytearray(CMD_FRAME_BYTES)
    f[0] = index & 0xFF
    struct.pack_into(">HB", f, 1, offset, count)
    struct.pack_into(">I", f, 4, OP_READ_EEPROM)
    return f


def frame_opcode(frame):
    return struct.unpack_from(">I", frame, 4)[0]


def check_opcode(op):
    """Raise RefusedOpcode unless `op` may be sent. Forbidden list first, so the
    refusal names the danger even if the allowlist were ever widened."""
    if op in FORBIDDEN_OPCODES:
        raise RefusedOpcode(f"opcode {op} refused: {FORBIDDEN_OPCODES[op]}")
    if op not in ALLOWED_OPCODES:
        raise RefusedOpcode(f"opcode {op} refused: not on the RX-only allowlist")


def check_frame(frame):
    """Validate a whole frame (length, opcode, padding) before it may be sent."""
    if len(frame) != CMD_FRAME_BYTES:
        raise RefusedOpcode(f"frame is {len(frame)} bytes, expected {CMD_FRAME_BYTES}")
    op = frame_opcode(frame)
    check_opcode(op)
    if op != OP_READ_EEPROM and any(frame[1:4]):
        raise RefusedOpcode(f"opcode {op}: bytes 1..3 must be zero")
    if any(frame[16:20]):
        raise RefusedOpcode(f"opcode {op}: bytes 16..19 must be zero")
    if op == OP_READ_EEPROM and not 1 <= frame[3] <= 32:
        raise RefusedOpcode(f"EEPROM read count {frame[3]} outside 1..32")
    return op


def send_frame(write, frame, index):
    """THE ONLY PATH THAT WRITES TO THE RADIO.

    `write(endpoint, data)` is the transport (the adapter passes a libusb
    interrupt write; tests pass a recorder). The frame is fully checked first;
    a refused frame raises RefusedOpcode and `write` is never called. The
    endpoint is fixed here to EP_CMD_OUT — callers cannot choose it.
    """
    check_frame(frame)
    frame[0] = index & 0xFF
    write(EP_CMD_OUT, bytes(frame))


def decode_reply(buf):
    """Interrupt-IN message -> (status, kind, index, value, data).

    status = byte 0 (active-low bits), kind = byte 1, index = byte 2.
    kind 1: value = BE u32 from bytes 4..7. kind 2: data = bytes from 4.
    Anything shorter than 3 bytes -> None.
    """
    if buf is None or len(buf) < 3:
        return None
    status, kind, index = buf[0], buf[1], buf[2]
    value, data = None, None
    if kind == REPLY_U32 and len(buf) >= 8:
        value = struct.unpack_from(">I", buf, 4)[0]
    elif kind == REPLY_EEPROM:
        data = bytes(buf[4:])
    return status, kind, index, value, data


# --- I/Q -----------------------------------------------------------------------
def iq_from_packet(buf, np=None):
    """int16-LE interleaved I,Q -> complex samples in [-1, 1), CONJUGATED.

    I/Q handedness on this radio is inverted (measured 2026-11-01: with the LO
    12 kHz below the 38.4 MHz TCXO spur, the spur appeared at -12 kHz instead of
    +12 kHz). Negating Q puts a carrier above the LO at a positive offset, which
    is AE's convention. With numpy returns a complex64 array; without, a list.
    """
    if np is not None:
        s = np.frombuffer(bytes(buf), dtype="<i2").astype(np.float32)
        s = s[: (len(s) // 2) * 2].reshape(-1, 2)
        return ((s[:, 0] - 1j * s[:, 1]) / FULL_SCALE).astype(np.complex64)
    n = (len(buf) // 4) * 4
    return [complex(i, -q) / FULL_SCALE for i, q in struct.iter_unpack("<hh", bytes(buf[:n]))]


# --- command channel (index-matched request/reply over the one write path) -------
class CommandChannel:
    """Owns EP4. Every frame goes through send_frame(); replies arrive via
    on_interrupt_in() (fed by the adapter's interrupt-IN callback) and are
    matched to requests by index.

    Thread-safe: the engine's control thread sends while libusb's event thread
    delivers replies.
    """

    def __init__(self, write):
        self._write = write
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._next = 1
        self._replies = {}
        self.writes_ok = 0
        self.writes_failed = 0
        self.refused = 0
        self.last_status = None
        self.replies_seen = 0

    def _alloc_index(self):
        with self._lock:
            idx = self._next
            self._next = 1 if self._next >= 255 else self._next + 1   # 1..255, never 0
        with self._cv:
            self._replies.pop(idx, None)
        return idx

    def _send(self, frame):
        try:
            check_frame(frame)
        except RefusedOpcode:
            self.refused += 1
            raise
        idx = self._alloc_index()
        try:
            send_frame(self._write, frame, idx)
        except RefusedOpcode:
            self.refused += 1
            raise
        except Exception:
            self.writes_failed += 1
            raise
        self.writes_ok += 1
        return idx

    def _wait(self, idx, timeout):
        with self._cv:
            self._cv.wait_for(lambda: idx in self._replies, timeout)
            return self._replies.pop(idx, None)

    def write_op(self, op, p1=0, p2=0):
        """Setter: send and do not wait (these opcodes have no read-back)."""
        self._send(encode_command(op, p1, p2))

    def read_op(self, op, p1=0, p2=0, timeout=0.4):
        """Query: send, wait for the index-matched type-1 reply. None on timeout."""
        idx = self._send(encode_command(op, p1, p2))
        r = self._wait(idx, timeout)
        return None if r is None or r[1] != REPLY_U32 else r[3]

    def read_eeprom(self, offset, count, timeout=0.4):
        idx = self._send(encode_eeprom_read(offset, count))
        r = self._wait(idx, timeout)
        return None if r is None or r[1] != REPLY_EEPROM else r[4][:count]

    def on_interrupt_in(self, buf):
        r = decode_reply(buf)
        if r is None:
            return
        self.last_status = r[0]
        if r[1] in (REPLY_U32, REPLY_EEPROM):
            self.replies_seen += 1
            with self._cv:
                self._replies[r[2]] = r
                self._cv.notify_all()


# --- offset tuning: where the hardware LO goes (pure, stdlib) -------------------
#
# The codec's DC offset (measured -147 / +55 counts on I / Q with no antenna,
# ~70 dB over the pan's median) sits exactly at the hardware LO. The engine
# centres AE's pan on the slice (native_centered_scope), so an LO equal to the
# pan centre puts every slice on top of it. By default (IF_OFFSET_HZ = 0) that
# is what happens and the DC block (dsp.DcBlocker) removes the line: the pan is
# then all real data. Opt-in offset tuning instead moves the LO off the
# slice's audio passband and the adapter shifts the IQ digitally so the pan
# stays labelled for the centre AE asked for. Prior art: PowerSDR (GPL, app
# code console.cs) tuned the FLEX-1500's DDS 3.8 kHz off the dial and shifted
# digitally, so the DC line stayed visible but outside the passband. This is
# our own implementation of that idea, not a copy.
#
# The cost is honest and fixed: the radio samples LO +/- 24 kHz, the pan is
# centre +/- 24 kHz, so |LO - centre| Hz at one edge of the pan were never
# sampled. The adapter zeroes that edge (dsp.mask_unsampled) rather than let the
# wrapped alias paint signals that are not there.

IF_OFFSET_HZ = 0               # default: LO on the slice (opt-in offset: 2000..16000)
IF_OFFSET_MIN_HZ = 2_000       # 0 = off; otherwise within these bounds
IF_OFFSET_MAX_HZ = 16_000
LO_GUARD_HZ = 1_500            # the passband may come no closer to the LO than this
LO_DEAD_EXTRA_HZ = 6_000       # hysteresis: tolerate |LO - centre| up to offset + this
LO_USABLE_HZ = 20_000          # passband must stay inside LO +/- this (codec roll-off)
SSB_PASS_HZ = 3_000            # the demod's sideband filter width (adapter get_audio)

LOWER_MODES = ("LSB", "DIGL", "CWL")
SYMMETRIC_MODES = ("AM", "SAM", "FM", "NFM", "DFM", "FM-N")


def check_if_offset(hz):
    """The IF offset in whole Hz; 0 turns offset tuning off. ValueError otherwise."""
    hz = int(hz)
    if hz != 0 and not IF_OFFSET_MIN_HZ <= hz <= IF_OFFSET_MAX_HZ:
        raise ValueError(f"IF offset {hz} Hz: use 0 (off) or "
                         f"{IF_OFFSET_MIN_HZ}..{IF_OFFSET_MAX_HZ} Hz")
    return hz


def mode_passband(mode):
    """(low, high) offsets from the slice that the LO policy keeps clear, and the
    side the LO goes on (-1 below the slice, +1 above).

    Upper-sideband family (USB, DIGU, CW, anything unknown) -> 0..+3 kHz, LO
    below; lower family -> -3..0 kHz, LO above. AM/FM -> +/-3 kHz, LO below
    (either side works; below was picked). The adapter's demod is SSB-only, so
    +/-3 kHz covers what it actually passes for those modes.
    """
    m = (mode or "").upper()
    if m in LOWER_MODES:
        return (-SSB_PASS_HZ, 0), +1
    if m in SYMMETRIC_MODES:
        return (-SSB_PASS_HZ, SSB_PASS_HZ), -1
    return (0, SSB_PASS_HZ), -1


def lo_clearance(lo, slice_hz, mode):
    """Hz between the LO and the nearest passband edge; negative if inside it."""
    (a, b), _ = mode_passband(mode)
    lo_edge, hi_edge = slice_hz + a, slice_hz + b
    if lo < lo_edge:
        return lo_edge - lo
    if lo > hi_edge:
        return lo - hi_edge
    return -min(lo - lo_edge, hi_edge - lo)


def lo_ok(lo, center_hz, slice_hz, mode, offset_hz):
    """True if an LO of `lo` can stay put: in coverage, the passband clear of it
    by LO_GUARD_HZ and inside its usable window, and the unsampled pan edge no
    wider than offset + LO_DEAD_EXTRA_HZ."""
    if lo is None or not in_rx_coverage(lo):
        return False
    if lo_clearance(lo, slice_hz, mode) < LO_GUARD_HZ:
        return False
    if abs(center_hz - lo) > offset_hz + LO_DEAD_EXTRA_HZ:
        return False
    (a, b), _ = mode_passband(mode)
    return max(abs(slice_hz + a - lo), abs(slice_hz + b - lo)) <= LO_USABLE_HZ


def choose_lo(center_hz, slice_hz, mode, cur_lo, offset_hz, relax=False):
    """The LO to request, or None to keep `cur_lo`.

    offset 0: today's behaviour, LO = pan centre (None if already there).
    Otherwise, hysteresis: a current LO that lo_ok() accepts is kept, so a slice
    drag moves the LO only every several kHz. When it must move (or `relax` asks
    for the preferred LO once the slice has settled) the candidates are, in
    order: centre on the mode's side, centre on the other side, the centre
    itself; the first lo_ok() one wins. If none passes, the in-coverage one with
    the most clearance. If none is in coverage at all, the centre, so the
    adapter's coverage check refuses it once and says so.
    """
    center_hz = float(center_hz)
    if not offset_hz:
        if cur_lo is not None and abs(cur_lo - center_hz) <= 1.0:
            return None
        return center_hz
    if not relax and lo_ok(cur_lo, center_hz, slice_hz, mode, offset_hz):
        return None
    _, side = mode_passband(mode)
    cands = (center_hz + side * offset_hz, center_hz - side * offset_hz, center_hz)
    good = [c for c in cands if lo_ok(c, center_hz, slice_hz, mode, offset_hz)]
    if good:
        pick = good[0]
    else:
        inside = [c for c in cands if in_rx_coverage(c)]
        pick = (max(inside, key=lambda c: lo_clearance(c, slice_hz, mode))
                if inside else center_hz)
    if cur_lo is not None and abs(pick - cur_lo) <= 1.0:
        return None
    return pick
