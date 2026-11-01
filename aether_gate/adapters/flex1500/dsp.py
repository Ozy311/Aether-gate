#
# Aether-gate — FLEX-1500 host DSP: DC block, digital frequency shift, honest edge.
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See LICENSE.
#
# bandpass_taps() generalises the complex one-sided SSB filter in
# adapters/soapy.py (_init_demod), and fm_discriminate() / BlockDcRemover follow
# its NBFM discriminator (_demod_fm), Copyright (C) 2026 Nigel Fenton (G0JKN),
# GPL-3.0-or-later.
#
"""Pure numpy helpers for the FLEX-1500 adapter. Every function takes the numpy
module as an argument (the adapter imports numpy only in open()), so nothing
here imports numpy at module load and the package still imports stdlib-only.
"""
import math

# DC-block corner. A one-pole high-pass on the block mean, ~1 Hz: the codec's
# DC offset is static (measured std ~0.9 LSB around it, no drift seen), so a
# slow estimate removes it completely, and a 1 Hz corner cannot touch audio —
# the lowest SSB audio anyone filters for is ~100 Hz, two decades up. With the
# LO offset on, the notch is not even in the passband.
DC_CORNER_HZ = 1.0


class DcBlocker:
    """Removes a slowly varying DC term from complex IQ, I and Q independently
    (the real and imaginary parts of the complex mean are separate estimates).

    Block-wise, not per sample: each block's mean pulls a running estimate
    with a = 1 - exp(-2*pi*fc*N/fs), the per-block step of a one-pole low-pass
    at fc, and the estimate is subtracted. Same idea as the FM discriminator's
    DC block in adapters/soapy.py. reset() reseeds from the next block (after a
    retune or preamp change the codec's DC moves).
    """

    def __init__(self, fs, fc_hz=DC_CORNER_HZ):
        self.fs = float(fs)
        self.fc_hz = float(fc_hz)
        self.dc = None             # complex, full-scale units, AS STORED (Q negated)

    def reset(self):
        self.dc = None

    def update(self, x, np):
        """Fold block `x` into the estimate; returns the estimate."""
        m = complex(np.mean(x)) if len(x) else 0j
        if self.dc is None:
            self.dc = m
        else:
            a = 1.0 - math.exp(-2.0 * math.pi * self.fc_hz * len(x) / self.fs)
            self.dc += a * (m - self.dc)
        return self.dc

    def process(self, x, np):
        """Update with `x` and return `x` minus the estimate (complex64)."""
        dc = self.update(x, np)
        return (x - np.complex64(dc)).astype(np.complex64)


def nco_shift(x, shift_hz, start_idx, fs, np):
    """Multiply by exp(+j*2*pi*shift*k/fs), k = start_idx + 0..len-1.

    The phase is a function of the absolute sample index, not of the block, so
    shifting two adjacent blocks separately equals shifting them joined:
    phase-continuous across any block boundary. A component at baseband f
    lands at f + shift_hz.
    """
    if not shift_hz or not len(x):
        return x
    k = np.arange(len(x), dtype=np.float64) + float(start_idx)
    # reduce the cycle count before scaling by 2*pi to keep float64 precision
    cyc = np.mod(k * (float(shift_hz) / float(fs)), 1.0)
    return (np.asarray(x, dtype=np.complex128) * np.exp(2j * np.pi * cyc))


def unsampled_band(shift_hz, fs):
    """(lo, hi) baseband range, Hz, that a shift of `shift_hz` leaves without
    real data (the radio sampled +/- fs/2 around its LO), or None if no shift."""
    if not shift_hz:
        return None
    half = fs / 2.0
    d = float(shift_hz)
    if abs(d) >= fs:
        return (-half, half)
    if d < 0:
        return (half - abs(d), half)
    return (-half, -half + d)


def mask_unsampled(x, shift_hz, fs, np):
    """Zero the part of a shifted block that holds wrapped alias, not data.

    After a shift by d, baseband content that was at the far edge has moved
    past +/- fs/2 and wrapped to the other edge, where it would read as a signal
    at a frequency the radio never sampled. Zero those FFT bins and transform
    back, so that edge of the pan shows the floor ("not looked at"), not alias.
    """
    band = unsampled_band(shift_hz, fs)
    if band is None or not len(x):
        return x
    X = np.fft.fft(x)
    f = np.fft.fftfreq(len(x), 1.0 / fs)
    lo, hi = band
    if abs(shift_hz) >= fs:
        X[:] = 0
    elif shift_hz < 0:
        X[f >= lo] = 0
    else:
        X[f < hi] = 0
    return np.fft.ifft(X)


DEMOD_TAPS = 255          # audio-rate filter length: Hamming, transition ~3.3*fs/N
                         # = ~310 Hz at 24 kHz, so a 500 Hz CW filter is a filter
                         # (soapy's 63 taps give ~1.25 kHz, wider than the CW band)


def bandpass_taps(rate, lo_hz, hi_hz, np, ntaps=DEMOD_TAPS):
    """Complex band-pass passing lo_hz..hi_hz (Hz, either sign; negative = below
    the carrier): a Hamming low-pass of half-width (hi-lo)/2 shifted up to the
    band centre (lo+hi)/2. soapy's SSB filter is the case lo=0, hi=3000."""
    lo_hz, hi_hz = float(min(lo_hz, hi_hz)), float(max(lo_hz, hi_hz))
    k = np.arange(ntaps) - (ntaps - 1) / 2.0
    half = ((hi_hz - lo_hz) / 2.0) / rate
    centre = ((hi_hz + lo_hz) / 2.0) / rate
    lp = np.sinc(2 * half * k) * np.hamming(ntaps)
    lp = lp / lp.sum()
    return (lp * np.exp(2j * np.pi * centre * k)).astype(np.complex128)


class BlockDcRemover:
    """Per-block one-pole high-pass on a real audio signal: subtract a running
    estimate of the block mean, a = 1 - exp(-2*pi*fc*N/fs) per block. soapy's FM
    DC block (_demod_fm), vectorised the same way; used after the AM envelope
    (the carrier becomes DC) and the FM discriminator (a tuning error does)."""

    def __init__(self, rate, fc_hz=10.0):
        self.rate = float(rate)
        self.fc_hz = float(fc_hz)
        self.dc = None

    def process(self, x, np):
        if not len(x):
            return x
        m = float(np.mean(x))
        if self.dc is None:
            self.dc = m
        else:
            a = 1.0 - math.exp(-2.0 * math.pi * self.fc_hz * len(x) / self.rate)
            self.dc += a * (m - self.dc)
        return x - self.dc


def fm_discriminate(z, prev, np):
    """Quadrature discriminator angle(z[n] * conj(z[n-1])) / pi, carrying the
    previous block's last sample (`prev`) so there is no tick at the block
    boundary. Returns (audio in -1..+1, new prev). soapy _demod_fm, including
    its full-range scaling (divide by pi, so noise cannot clip)."""
    if not len(z):
        return np.zeros(0, dtype=np.float64), prev
    p = np.concatenate([[prev], z[:-1]])
    return np.angle(z * np.conj(p)) * (1.0 / np.pi), z[-1]
