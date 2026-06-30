#
# Aether-gate — SoapySDR adapter: live IQ from any SoapySDR device (RTL-SDR first).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""SoapyRTLSDR / SoapySDR IQ adapter — a `provides="iq"` source.

This is the real-hardware adapter and the fix for the PoC's ~1 fps: instead of
spawning `rtl_sdr` once per frame (re-opening USB + re-locking the tuner PLL each
time, ~0.8 s of pure overhead), it opens the device ONCE and runs a persistent
`readStream` loop on a background thread. The tuner stays locked, samples flow
continuously, and `get_iq()` just hands the core the latest block to FFT.

Covers any SoapySDR device via `--soapy-driver` (rtlsdr, airspy, sdrplay, ...);
RTL-SDR Blog V4 is the default/first target.

Dependency: the SoapySDR Python binding (`import SoapySDR`) + the device's Soapy
module (e.g. SoapyRTLSDR). Import is deferred to open() so the package stays
importable on hosts without Soapy (tests, the sim adapter).
"""
import collections
import threading
import time

from .base import RadioAdapter, AdapterCaps, Meters

AUDIO_RATE = 24000          # AE remote_audio_rx rate (must match core AUDIO_RATE)
SSB_BW_HZ = 2700.0          # SSB audio passband width


class SoapyAdapter(RadioAdapter):
    """Live IQ from a SoapySDR device. The core runs the FFT (provides='iq')."""

    provides = "iq"

    def __init__(self, driver="rtlsdr", device_args="", samp_rate=2_048_000,
                 gain_db=40.0, center_hz=14_100_000.0, model="FLEX-6700",
                 serial="GATE0001", station="aether-gate 1", direct_samp=None, agc=False):
        self.driver = driver
        self.device_args = device_args
        self.samp_rate = float(samp_rate)
        self.gain_db = float(gain_db)
        self.center_hz = float(center_hz)
        self.direct_samp = direct_samp      # RTL direct-sampling mode (Q=2 for HF on non-V4); None=auto
        self.agc = agc
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station, tx_capable=False,
                                        min_span_hz=48_000.0, max_span_hz=samp_rate)
        self._sdr = None
        self._stream = None
        self._lock = threading.Lock()
        self._latest = None                 # most recent complex block (for the panadapter FFT)
        self._run = False
        self._reader = None
        self._retune_to = None              # pending centre change (applied in the reader thread)
        self._np = None
        # --- demod / audio state (SSB first) ---
        self._slice_hz = center_hz          # where to demodulate (the slice freq; core updates it)
        self._mode = "USB"                  # USB/LSB (others -> default to USB for now)
        self._audio_q = collections.deque(maxlen=64)  # raw IQ blocks queued for the demodulator
        self._nco_phase = 0.0               # persistent mixer phase (continuity across blocks)
        self._decim = None                  # samp_rate / AUDIO_RATE (integer-ish); set in open()
        self._lpf = None                    # decimating FIR taps
        self._lpf_state = None              # FIR overlap state
        self._iq_resid = None               # leftover IQ samples between audio calls
        self._audio_gain = 8.0              # post-demod gain (SSB audio is quiet); tweakable

    # --- lifecycle -------------------------------------------------------
    def open(self):
        import numpy as np                  # hard deps only when really running hardware
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
        self._np = np
        self._SOAPY_SDR_RX = SOAPY_SDR_RX
        self._SOAPY_SDR_CF32 = SOAPY_SDR_CF32

        args = dict(driver=self.driver)
        if self.device_args:
            for kv in self.device_args.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1); args[k] = v
        self._sdr = SoapySDR.Device(args)
        self._sdr.setSampleRate(SOAPY_SDR_RX, 0, self.samp_rate)
        self._sdr.setFrequency(SOAPY_SDR_RX, 0, self.center_hz)
        try:
            self._sdr.setGainMode(SOAPY_SDR_RX, 0, bool(self.agc))   # AGC on/off
        except Exception:
            pass
        if not self.agc:
            self._sdr.setGain(SOAPY_SDR_RX, 0, self.gain_db)
        if self.direct_samp is not None:                            # RTL HF direct-sampling (non-V4 dongles)
            try:
                self._sdr.writeSetting("direct_samp", str(self.direct_samp))
            except Exception:
                pass

        self._stream = self._sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self._sdr.activateStream(self._stream)

        # --- demod setup: design a decimating low-pass (samp_rate -> AUDIO_RATE) ---
        self._decim = max(1, int(round(self.samp_rate / AUDIO_RATE)))
        # cutoff = SSB bandwidth, normalised to the input rate; windowed-sinc FIR (numpy only)
        cutoff = SSB_BW_HZ / self.samp_rate
        ntaps = 64 * self._decim if self._decim < 64 else 4096   # enough taps for sharp anti-alias
        ntaps = min(ntaps, 8192)
        m = (ntaps - 1) / 2.0
        idx = np.arange(ntaps) - m
        h = np.sinc(2 * cutoff * idx) * np.hamming(ntaps)        # LP prototype
        self._lpf = (h / h.sum()).astype(np.float64)
        self._lpf_state = np.zeros(ntaps - 1, dtype=np.complex128)
        self._iq_resid = np.zeros(0, dtype=np.complex64)

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._run = False
        if self._reader:
            self._reader.join(timeout=2)
        try:
            if self._stream is not None:
                self._sdr.deactivateStream(self._stream)
                self._sdr.closeStream(self._stream)
        except Exception:
            pass
        self._sdr = self._stream = None

    # --- the persistent reader (this is what kills the per-frame PLL re-lock) --
    def _read_loop(self):
        np = self._np
        CHUNK = 4096
        buf = np.empty(CHUNK, dtype=np.complex64)
        while self._run:
            # apply any pending retune on this thread (avoid racing readStream)
            if self._retune_to is not None:
                try:
                    self._sdr.setFrequency(self._SOAPY_SDR_RX, 0, float(self._retune_to))
                    self.center_hz = float(self._retune_to)
                except Exception:
                    pass
                self._retune_to = None
            sr = self._sdr.readStream(self._stream, [buf], CHUNK, timeoutUs=200000)
            n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
            if n > 0:
                block = buf[:n].copy()
                with self._lock:
                    self._latest = block        # for the panadapter FFT (latest is fine)
                self._audio_q.append(block)     # for the demod (continuous — every block consumed)
            elif n < 0:
                time.sleep(0.001)           # overflow/timeout — keep the stream alive, don't spin hot

    # --- control --------------------------------------------------------
    def retune(self, center_hz):
        # AE retunes the slice; the hardware centre follows the pan, but the slice
        # we DEMODULATE is this frequency (may be offset within the pan).
        self._slice_hz = float(center_hz)
        self._retune_to = float(center_hz)  # picked up by the reader thread

    def set_mode(self, mode):
        self._mode = (mode or "USB").upper()

    # --- the IQ source (core FFTs this) ---------------------------------
    def get_iq(self, n, center_hz, span_hz):
        # If AE's centre moved, schedule the hardware to follow.
        if abs(center_hz - self.center_hz) > 1.0 and self._retune_to is None:
            self._retune_to = float(center_hz)
        with self._lock:
            blk = self._latest
        if blk is None:
            return None
        return blk                          # core/fft.iq_to_dbm resamples to n bins

    # --- the AUDIO source (SSB demod; numpy only) -----------------------
    def get_audio(self, n_samples, slice_hz=None, mode=None):
        """Return n_samples of 24 kHz mono audio (float, ~[-1,1]) demodulated from
        the live IQ at the slice frequency. None if not enough IQ buffered yet."""
        np = self._np
        if np is None or self._lpf is None:
            return None
        if slice_hz is not None:
            self._slice_hz = float(slice_hz)
        if mode is not None:
            self._mode = mode.upper()

        # how many input samples we need for n_samples output after decimation
        need_in = n_samples * self._decim
        # drain queued IQ blocks into the residual buffer until we have enough
        while len(self._iq_resid) < need_in and self._audio_q:
            self._iq_resid = np.concatenate([self._iq_resid, self._audio_q.popleft()])
        if len(self._iq_resid) < need_in:
            return None                      # not enough IQ yet (stream still filling)

        iq = self._iq_resid[:need_in].astype(np.complex128)
        self._iq_resid = self._iq_resid[need_in:]

        # 1) mix the slice down to baseband: shift by (slice - hardware centre)
        f_off = self._slice_hz - self.center_hz
        k = np.arange(len(iq))
        ph = self._nco_phase + 2.0 * np.pi * (-f_off) / self.samp_rate * k
        iq = iq * np.exp(1j * ph)
        self._nco_phase = (ph[-1] + 2.0 * np.pi * (-f_off) / self.samp_rate) % (2.0 * np.pi)

        # 2) anti-alias low-pass (complex FIR with overlap state), then decimate
        x = np.concatenate([self._lpf_state, iq])
        filt = np.convolve(x, self._lpf, mode="valid")  # len == len(iq)
        self._lpf_state = iq[-(len(self._lpf) - 1):]
        base = filt[::self._decim][:n_samples]

        # 3) SSB demod: USB = real part of the (already lowpassed) baseband; for LSB
        #    conjugate first (mirrors the sideband). Real part recovers the audio.
        if self._mode.startswith("LSB"):
            audio = np.real(np.conj(base))
        else:                                # USB / DIGU / default
            audio = np.real(base)

        audio = audio * self._audio_gain
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio.tolist()

    def read_meters(self):
        return Meters()
