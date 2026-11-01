#
# Aether-gate — FLEX-1500 adapter: RX-only live IQ over USB (libusb).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See LICENSE.
#
# The SSB demod (get_audio / _factor_decim / the stage-FIR setup in open()) is
# adapted from adapters/hpsdr/adapter.py, Copyright (C) 2026 Nigel Fenton
# (G0JKN), GPL-3.0-or-later. Copied rather than shared so neither adapter's
# private state becomes the other's contract; a common host-DSP helper is the
# obvious later extraction.
#
"""FLEX-1500 IQ adapter — a `provides="iq"` source, RECEIVE ONLY.

The FLEX-1500 is a USB-tethered direct-conversion transceiver with no onboard
DSP: it streams 48 kHz complex baseband and takes 20-byte command frames. This
adapter claims interface 3, keeps iso IN transfers on EP 0x82 in flight, listens
on interrupt IN 0x83 for replies and the status byte, and tunes with three
volatile setters (RX1 filter, preamp, RX1 tuning word). The core runs the FFT.

TX IS OUT OF SCOPE. This adapter has no key_tx(), leaves wants_tx() at the base
class's None, advertises tx_capable=False, never submits to iso OUT 0x01 (the TX
I/Q endpoint) and cannot send T/R, EEPROM-write, initialise, raw-I2C or keying
opcodes — wire.send_frame() refuses them before any USB write. Adding TX needs
its own PR carrying the CONSTITUTION §I safety story (arm latch, band whitelist,
stuck-PTT watchdog, unkey on disconnect, and what happens when intent is
ambiguous).

§II — what is a reading and what is not. The firmware revision, serial number
and status byte are replies from the radio. The filter, preamp and LO are
COMMANDED values: the radio's SPI bus is outbound only, so there is no read-back
and diagnostics() labels them as such. A command whose USB write fails, or a
tune outside the radio's RX coverage, leaves the previous value in place.

Dependencies, both imported only in open() (like soapy/hpsdr), so the package
imports and its tests run without them:
  * libusb1 (pip; module `usb1`, LGPL-2.1+) and the libusb-1.0 system library;
  * numpy.
On Linux the device node is root-owned by default: run under sudo or add a udev
rule for 2192:1502 (the gate does not install one).
"""
import collections
import threading
import time

from ..base import RadioAdapter, AdapterCaps, Meters
from ...core.fft import dbm_offset_for   # stdlib-safe: numpy is optional there
from . import wire

AUDIO_RATE = 24_000          # AE remote_audio_rx rate (must match core AUDIO_RATE)
N_ISO_XFERS = 8              # iso transfers kept in flight
ISO_PKTS_PER_XFER = 16       # 16 x 1 ms packets per transfer (768 samples)
SETTLE_S = 0.15              # drop IQ for this long after a retune; our RX-only
                             # probe used 150 ms and saw the spur cleanly on
                             # every LO step
STALL_S = 0.5                # no iso packet for this long -> note_device_silent
PAN_RING_BLOCKS = 64         # ~49k samples (~1 s) of recent IQ for get_iq(n)
AUDIO_Q_BLOCKS = 128         # ~2 s of IQ queued for the demodulator
METER_SAMPLES = 4096

# The ham bands the radio's RX filters and TX coverage are built around
# (service manual / datasheet: 160-6 m), in AE BandDefs names.
BANDS = ("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m",
         "10m", "6m")


class Flex1500Adapter(RadioAdapter):
    """Live 48 kHz IQ from a FLEX-1500 over USB. RX only. The core runs the FFT."""

    provides = "iq"

    def __init__(self, preamp_db=0, center_hz=14_100_000.0, model="FLEX-6600",
                 serial="", station="FLEX-1500"):
        self.samp_rate = wire.SAMPLE_RATE
        self.preamp_db = int(preamp_db)
        wire.preamp_code(self.preamp_db)           # refuse a non-step at construction
        self.center_hz = float(center_hz)          # last LO the radio ACCEPTED
        self.lo_hz = None                          # quantised LO of that tuning word
        self.filter_index = None                   # last RX1 filter accepted
        self._serial_override = serial
        # FLEX-6600 = HF + 6 m (RADIO_SUPPORT.md). Span is the real 48 kHz: one
        # sample rate, no zoom in hardware, so min == max. The pan is always
        # centred on the LO (direct conversion), hence native_centered_scope.
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station,
                                        tx_capable=False, native_centered_scope=True,
                                        min_span_hz=float(wire.SAMPLE_RATE),
                                        max_span_hz=float(wire.SAMPLE_RATE),
                                        bands=BANDS)
        self._usb1 = None
        self._np = None
        self._ctx = None
        self._handle = None
        self._claimed = False
        self._chan = None
        self._iso = []
        self._int_xfer = None
        self._run = False
        self._event_thread = None
        self._ctl_thread = None
        self._lock = threading.Lock()
        self._pan_ring = collections.deque(maxlen=PAN_RING_BLOCKS)
        self._latest = None                        # newest block (engine profiling reads it)
        self._settle_until = 0.0
        self._retune_to = None
        self._ae_center_hz = None
        self._seeded = False                       # engine clears it on each AE connect
                                                   # (issue #31): first get_iq() retunes
                                                   # even onto the current centre
        self._last_pkt = 0.0
        self.fw_rev = None
        self.serial_num = None
        self.trx_sn = None
        self.iso_packets = 0
        self.iso_errors = 0
        self.iso_resubmit_errors = 0
        self.int_errors = 0
        self.tune_ok = 0
        self.tune_refused = 0
        self.tune_failed = 0
        self.last_refused_hz = None
        # --- audio / SSB demod state (from hpsdr) ---
        self._slice_hz = self.center_hz
        self._mode = "USB"
        self._audio_q = collections.deque(maxlen=AUDIO_Q_BLOCKS)
        self._nco_phase = 0.0
        self._decim = None
        self._stage_firs = []
        self._iq_resid = None
        self._audio_gain = 4.0
        self._agc_level = 0.05
        self._agc_target = 0.25

    # --- transport: the one EP 0x04 write, handed to wire.send_frame ----------
    def _usb_write(self, endpoint, data):
        """Transport for wire.CommandChannel. send_frame() has already refused
        anything off the allowlist and fixed the endpoint; this re-asserts it."""
        if endpoint != wire.EP_CMD_OUT:
            raise wire.RefusedOpcode(f"write to endpoint 0x{endpoint:02x} refused")
        n = self._handle.interruptWrite(endpoint, data, timeout=500)
        if n != len(data):
            raise OSError(f"short interrupt write: {n}/{len(data)} bytes")

    # --- lifecycle -------------------------------------------------------
    def open(self):
        try:
            import usb1
        except ImportError as e:
            raise RuntimeError(
                "the flex1500 adapter needs the 'libusb1' Python package "
                "(pip install libusb1) and the libusb-1.0 library") from e
        try:
            import numpy as np
        except ImportError as e:
            raise RuntimeError("the flex1500 adapter needs numpy (pip install numpy)") from e
        self._usb1, self._np = usb1, np

        self._ctx = usb1.USBContext()
        self._ctx.open()
        try:
            h = self._ctx.openByVendorIDAndProductID(wire.VID, wire.PID,
                                                     skip_on_error=False)
        except usb1.USBErrorAccess as e:
            raise RuntimeError(
                f"permission denied opening the FLEX-1500 ({wire.VID:04x}:{wire.PID:04x}); "
                f"run the gate with access to the USB device node or add a udev rule") from e
        if h is None:
            raise RuntimeError(f"no FLEX-1500 ({wire.VID:04x}:{wire.PID:04x}) found on USB "
                               f"(is it powered on and plugged in?)")
        self._handle = h
        h.claimInterface(wire.INTERFACE)
        self._claimed = True
        self._chan = wire.CommandChannel(self._usb_write)

        self._run = True
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True,
                                              name="flex1500-usb")
        self._event_thread.start()

        # Replies and the status byte arrive on interrupt IN; arm it first.
        t = h.getTransfer()
        t.setInterrupt(wire.EP_INT_IN, wire.INT_IN_BYTES, callback=self._on_int, timeout=0)
        t.submit()
        self._int_xfer = t

        # Firmware gate BEFORE any setter: a radio outside the known range may
        # not speak this protocol, and we will not guess.
        fw = self._chan.read_op(wire.OP_GET_FIRMWARE_REV)
        if fw is None:
            raise RuntimeError("FLEX-1500 did not answer GET_FIRMWARE_REV")
        self.fw_rev = fw
        if not wire.fw_rev_ok(fw):
            raise RuntimeError(f"FLEX-1500 firmware {wire.fw_rev_str(fw)} (0x{fw:08x}) is "
                               f"outside the accepted 0x{wire.FW_MIN:08x}..0x{wire.FW_MAX:08x}; "
                               f"refusing to drive it")
        self.note_device_alive()
        self.serial_num = self._chan.read_op(wire.OP_GET_SERIAL_NUM)
        self.trx_sn = self._chan.read_op(wire.OP_GET_TRX_SN)
        if not self._serial_override and self.serial_num is not None:
            # Raw u32 in hex: the radio's reply, not a reformatting of it.
            self.capabilities.serial = f"{self.serial_num:08X}"

        # --- demod setup: staged anti-alias + decimate 48 kHz -> 24 kHz (hpsdr) ---
        self._decim = max(1, int(round(self.samp_rate / AUDIO_RATE)))
        self._stage_firs = []
        for M in self._factor_decim(self._decim):
            ntaps = 4 * M + 1
            cutoff = 0.45 / M
            idx = np.arange(ntaps) - (ntaps - 1) / 2.0
            taps = np.sinc(2 * cutoff * idx) * np.hamming(ntaps)
            taps = (taps / taps.sum()).astype(np.float64)
            self._stage_firs.append([taps, np.zeros(ntaps - 1, dtype=np.complex128), M])
        self._iq_resid = np.zeros(0, dtype=np.complex64)

        # Start the IQ stream: submitting iso IN transfers is all it takes.
        for _ in range(N_ISO_XFERS):
            x = h.getTransfer(iso_packets=ISO_PKTS_PER_XFER)
            x.setIsochronous(wire.EP_ISO_IN, wire.ISO_PACKET_BYTES * ISO_PKTS_PER_XFER,
                             callback=self._on_iso, timeout=1000)
            x.submit()
            self._iso.append(x)
        self._last_pkt = time.monotonic()

        self._chan.write_op(wire.OP_SET_TRX_PREAMP, wire.preamp_code(self.preamp_db))
        start_hz = self.center_hz
        self.center_hz = None
        if not self._apply_tune(start_hz):
            raise RuntimeError(f"could not tune the FLEX-1500 to {start_hz / 1e6:.6f} MHz")

        self._ctl_thread = threading.Thread(target=self._ctl_loop, daemon=True,
                                            name="flex1500-ctl")
        self._ctl_thread.start()
        print(f"[flex1500] firmware {wire.fw_rev_str(fw)}, serial "
              f"{self.capabilities.serial or '?'}, LO {self.center_hz / 1e6:.6f} MHz, "
              f"filter {self.filter_index}, preamp {self.preamp_db:+d} dB (RX only)",
              flush=True)

    def close(self):
        self._run = False
        if self._ctl_thread is not None:
            self._ctl_thread.join(timeout=1)
        xfers = self._iso + ([self._int_xfer] if self._int_xfer else [])
        for x in xfers:
            try:
                x.cancel()
            except Exception:
                pass
        # The event thread keeps pumping until the cancellations have landed
        # (or a 1 s cap), so the interface is never released under live transfers.
        if self._event_thread is not None:
            self._event_thread.join(timeout=2)
        if self._handle is not None:
            if self._claimed:
                try:
                    self._handle.releaseInterface(wire.INTERFACE)
                except Exception:
                    pass
            try:
                self._handle.close()
            except Exception:
                pass
        self._handle = None
        self._claimed = False
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        self._ctx = None

    @staticmethod
    def _xfer_submitted(x):
        try:
            return x.isSubmitted()
        except Exception:
            return False

    # --- libusb event thread + callbacks ----------------------------------
    def _event_loop(self):
        drain_until = None
        while True:
            try:
                self._ctx.handleEventsTimeout(0.1)
            except Exception:
                time.sleep(0.05)
            if self._run:
                continue
            if drain_until is None:
                drain_until = time.monotonic() + 1.0
            xfers = self._iso + ([self._int_xfer] if self._int_xfer else [])
            if time.monotonic() > drain_until or not any(self._xfer_submitted(x) for x in xfers):
                break

    def _on_iso(self, x):
        usb1 = self._usb1
        st = x.getStatus()
        if st == usb1.TRANSFER_COMPLETED:
            good = []
            for pst, buf in x.iterISO():
                if pst != usb1.TRANSFER_COMPLETED or not len(buf):
                    self.iso_errors += 1
                    continue
                good.append(bytes(buf))
            if good:
                self.iso_packets += len(good)
                self._last_pkt = time.monotonic()
                self.note_device_alive()
                if time.monotonic() >= self._settle_until:
                    blk = wire.iq_from_packet(b"".join(good), self._np)
                    with self._lock:
                        self._pan_ring.append(blk)
                        self._latest = blk
                    self._audio_q.append(blk)
        elif st == usb1.TRANSFER_CANCELLED:
            return
        elif st == usb1.TRANSFER_NO_DEVICE:
            self._lost("the FLEX-1500 left the USB bus (unplugged or powered off)")
            return
        else:
            self.iso_errors += 1
        if self._run:
            try:
                x.submit()
            except usb1.USBErrorNoDevice:
                self._lost("the FLEX-1500 left the USB bus (unplugged or powered off)")
            except Exception:
                self.iso_resubmit_errors += 1

    def _on_int(self, x):
        usb1 = self._usb1
        st = x.getStatus()
        if st == usb1.TRANSFER_COMPLETED:
            self._chan.on_interrupt_in(x.getBuffer()[:x.getActualLength()])
        elif st == usb1.TRANSFER_CANCELLED:
            return
        elif st == usb1.TRANSFER_NO_DEVICE:
            self._lost("the FLEX-1500 left the USB bus (unplugged or powered off)")
            return
        elif st != usb1.TRANSFER_TIMED_OUT:        # timeouts are normal on 0x83
            self.int_errors += 1
        if self._run:
            try:
                x.submit()
            except Exception:
                self.int_errors += 1

    def _lost(self, reason):
        if self.note_device_silent(reason, after_s=0):
            print(f"[flex1500] {reason} — treating the device as lost", flush=True)

    # --- control thread: applies retunes off the engine's frame loop --------
    def _ctl_loop(self):
        while self._run:
            hz = self._retune_to
            if hz is not None:
                self._retune_to = None
                self._apply_tune(hz)
            # Async iso simply stops calling back when the device vanishes, so
            # staleness is checked here, not only inside the callbacks.
            if time.monotonic() - self._last_pkt > STALL_S:
                if self.note_device_silent("the FLEX-1500 stopped streaming IQ "
                                           "(unplugged, powered off or USB stalled)"):
                    print(f"[flex1500] no IQ for {self.device_lost_after_s:.0f}s — "
                          f"treating the device as lost", flush=True)
            time.sleep(0.02)

    def _apply_tune(self, hz):
        """Filter + tuning word for an LO of `hz`. True only if the radio
        accepted both writes; on refusal or failure the previous LO stands."""
        hz = float(hz)
        if not wire.in_rx_coverage(hz):
            self.tune_refused += 1
            if self.last_refused_hz != hz:
                self.last_refused_hz = hz
                was = f"{self.center_hz / 1e6:.6f} MHz" if self.center_hz else "unset"
                print(f"[flex1500] tune {hz / 1e6:.6f} MHz refused: outside RX coverage "
                      f"{wire.RX_MIN_HZ / 1e6:.2f}-{wire.RX_MAX_HZ / 1e6:.0f} MHz; "
                      f"LO stays {was}", flush=True)
            return False
        idx = wire.rx_filter_index(hz)
        tw = wire.tuning_word(hz)
        try:
            self._chan.write_op(wire.OP_SET_RX1_FILTER, idx)
            self.filter_index = idx
            self._chan.write_op(wire.OP_SET_RX1_FREQ_TW, tw, 0)
        except wire.RefusedOpcode:
            raise
        except Exception as e:
            self.tune_failed += 1
            print(f"[flex1500] tune {hz / 1e6:.6f} MHz failed: {e}", flush=True)
            return False
        self.center_hz = hz
        self.lo_hz = wire.lo_hz_from_tw(tw)
        self.tune_ok += 1
        with self._lock:
            self._pan_ring.clear()
            self._latest = None
        self._audio_q.clear()
        self._settle_until = time.monotonic() + SETTLE_S
        return True

    # --- control (AE -> radio) ------------------------------------------
    def retune(self, center_hz):
        self._retune_to = float(center_hz)

    def set_span(self, span_hz):
        """One sample rate, so the span is always 48 kHz whatever AE asks."""
        return float(self.samp_rate)

    def current_span_hz(self):
        """The IQ width to advertise (= the sample rate). Without it AE draws
        48 kHz of data across the gate's default span and signals land at the
        wrong frequency."""
        return float(self.samp_rate)

    def set_slice(self, slice_hz):
        """Demod target. The pan is centred on the LO; if AE puts the slice far
        off centre (beyond 40% of the span), move the LO onto it."""
        self._slice_hz = float(slice_hz)
        if self.center_hz is None or abs(self._slice_hz - self.center_hz) > 0.40 * self.samp_rate:
            self._retune_to = self._slice_hz

    def set_mode(self, mode):
        if mode:
            self._mode = mode.upper()

    # --- readback (radio -> AE) -----------------------------------------
    def read_meters(self):
        """S-meter DERIVED from the IQ, not read from the radio (it has no
        meter). Uncalibrated: it goes through the same dBFS->dBm seam as the
        panadapter (core.fft.dbm_offset_for with the engine's defaults), so pan
        and meter agree with each other, not with a reference.

        The block mean is removed first: with no antenna the codec's DC offset
        (measured about -147 / +55 counts on I / Q) sits some 45 dB above the
        ~1 LSB noise, so a raw RMS would report the DC offset, not the signal.
        noise_dbm stays None — this adapter does not separate signal from noise.
        """
        return Meters(s_meter_dbm=self._s_meter_dbm())

    def _s_meter_dbm(self):
        np = self._np
        if np is None:
            return -140.0
        blk = self._recent(METER_SAMPLES)
        if blk is None or not len(blk):
            return -140.0
        x = blk - blk.mean()
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2))) + 1e-12
        dbm = 20.0 * np.log10(rms) + dbm_offset_for(20.0)
        return max(-140.0, min(0.0, dbm))

    def diagnostics(self):
        st = self._chan.last_status if self._chan else None
        d = {
            "radio": "FLEX-1500",
            "link": {"transport": "usb", "host": f"{wire.VID:04x}:{wire.PID:04x} iface {wire.INTERFACE}",
                     "state": "lost" if self.device_lost else ("streaming" if self._run else "closed"),
                     "detail": self.device_lost_reason or ""},
            "firmware": wire.fw_rev_str(self.fw_rev) if self.fw_rev is not None else None,
            "serial_num": f"0x{self.serial_num:08x}" if self.serial_num is not None else None,
            "trx_sn": f"0x{self.trx_sn:08x}" if self.trx_sn is not None else None,
            "commanded": {"lo_hz": self.lo_hz, "center_hz": self.center_hz,
                          "rx1_filter": self.filter_index, "preamp_db": self.preamp_db},
            "commanded_note": ("filter / preamp / LO are what the gate SENT and the "
                               "radio accepted over USB; the FLEX-1500 has no read-back "
                               "for them (outbound-only SPI)"),
            "status_byte": f"0x{st:02x}" if st is not None else None,
            "status_inputs": wire.decode_status(st) if st is not None else None,
            "samp_rate": self.samp_rate,
            "meters": {"s_meter_dbm": round(self._s_meter_dbm(), 1),
                       "note": "derived from IQ, uncalibrated"},
            "counters": {
                "iso_packets": self.iso_packets, "iso_errors": self.iso_errors,
                "iso_resubmit_errors": self.iso_resubmit_errors,
                "int_errors": self.int_errors,
                "cmd_writes_ok": self._chan.writes_ok if self._chan else 0,
                "cmd_writes_failed": self._chan.writes_failed if self._chan else 0,
                "cmd_refused": self._chan.refused if self._chan else 0,
                "replies": self._chan.replies_seen if self._chan else 0,
                "tune_ok": self.tune_ok, "tune_refused": self.tune_refused,
                "tune_failed": self.tune_failed},
            "tx": "not supported by this adapter (RX only)",
            "known_artifacts": ("38.4 MHz TCXO leaks into RX ~20 dB above the floor; a "
                                "fixed baseband pair near +/-19.4 kHz is not RF (does not "
                                "move with the LO)"),
        }
        return d

    # --- the IQ source --------------------------------------------------
    def _recent(self, n):
        """The newest n samples (or fewer, if that is all there is)."""
        with self._lock:
            blocks = list(self._pan_ring)
        if not blocks:
            return None
        take, have = [], 0
        for b in reversed(blocks):
            take.append(b)
            have += len(b)
            if have >= n:
                break
        blk = take[0] if len(take) == 1 else self._np.concatenate(list(reversed(take)))
        return blk[-n:]

    def get_iq(self, n, center_hz, span_hz):
        # Follow AE's centre, comparing AE-to-AE (soapy's lesson): comparing to
        # the hardware LO would re-request a refused tune on every frame.
        if not self._seeded:
            self._seeded = True
            self._ae_center_hz = float(center_hz)
            self.retune(center_hz)
        elif abs(center_hz - self._ae_center_hz) > 1.0:
            self._ae_center_hz = float(center_hz)
            self.retune(center_hz)
        # Serve the length asked for, so the advertised bin width is real
        # (CONSTITUTION §VI: never ignore n).
        return self._recent(max(1, int(n)))

    # --- the AUDIO source (SSB demod, adapted from hpsdr; numpy only) ------
    @staticmethod
    def _factor_decim(D):
        """Factor a decimation D into a few small stages (largest-first)."""
        factors = []
        for p in (5, 4, 3, 2):
            while D % p == 0 and D // p >= 1:
                factors.append(p); D //= p
        if D > 1:
            factors.append(D)
        return factors or [1]

    def get_audio(self, n_samples, slice_hz=None, mode=None):
        """n_samples of 24 kHz mono audio from the live IQ, or None until enough
        is buffered. Mix the slice to baseband, decimate 48k -> 24k, take the
        real part (USB) / conj-real (LSB), light AGC — as hpsdr does."""
        np = self._np
        if np is None or not self._stage_firs:
            return None
        if slice_hz is not None:
            self.set_slice(slice_hz)
        if mode is not None:
            self._mode = mode.upper()

        need_in = n_samples * self._decim
        while len(self._iq_resid) < need_in and self._audio_q:
            self._iq_resid = np.concatenate([self._iq_resid, self._audio_q.popleft()])
        if len(self._iq_resid) < need_in:
            return None
        iq = self._iq_resid[:need_in].astype(np.complex128)
        self._iq_resid = self._iq_resid[need_in:]

        f_off = self._slice_hz - (self.center_hz or self._slice_hz)
        k = np.arange(len(iq))
        ph = self._nco_phase + 2.0 * np.pi * (-f_off) / self.samp_rate * k
        iq = iq * np.exp(1j * ph)
        self._nco_phase = (ph[-1] + 2.0 * np.pi * (-f_off) / self.samp_rate) % (2.0 * np.pi)

        sig = iq
        for fir in self._stage_firs:
            taps, state, M = fir
            x = np.concatenate([state, sig])
            y = np.convolve(x, taps, mode="valid")
            fir[1] = sig[-(len(taps) - 1):]
            sig = y[::M]
        base = sig[:n_samples]
        if len(base) < n_samples:
            base = np.concatenate([base, np.zeros(n_samples - len(base), dtype=base.dtype)])

        if self._mode.startswith("LSB"):
            audio = np.real(np.conj(base))
        else:
            audio = np.real(base)

        audio = audio * self._audio_gain
        rms = float(np.sqrt(np.mean(audio * audio)) + 1e-9)
        a = 0.3 if rms > self._agc_level else 0.02
        self._agc_level = (1 - a) * self._agc_level + a * rms
        audio = audio * (self._agc_target / max(self._agc_level, 1e-4))
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio.tolist()
