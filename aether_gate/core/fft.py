#
# Aether-gate — IQ -> dBm spectrum transform (the core-side FFT for IQ adapters).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Core-side FFT so every IQ adapter shares one transform.

A spectrum adapter returns dBm bins directly; an IQ adapter returns complex
samples and the core calls iq_to_dbm() here. Keeping the transform in the core
(not in each adapter) is the point of the "narrow waist": one well-tested
IQ->panadapter path, not N.

numpy is used when available (the real path for SoapySDR/RTL-SDR IQ). A pure-
stdlib fallback keeps the core importable with no third-party deps so the sim
adapter and unit tests run anywhere; it is not meant to be fast.
"""
import math

try:
    import numpy as _np
except Exception:                                  # pragma: no cover - exercised when numpy absent
    _np = None


def iq_to_dbm(iq, n_bins, min_dbm, max_dbm):
    """Convert a block of complex IQ samples to `n_bins` dBm magnitudes.

    Windowed FFT -> fftshift (DC centre) -> 20*log10 magnitude -> clamp to the
    AE display range [min_dbm, max_dbm]. Returns a list of length n_bins.
    """
    if _np is not None:
        x = _np.asarray(iq, dtype=_np.complex128)
        if x.size == 0:
            return [min_dbm] * n_bins
        if x.size != n_bins:                       # resample length to the pan width
            idx = _np.linspace(0, x.size - 1, n_bins).astype(int)
            x = x[idx]
        win = _np.hanning(n_bins)
        spec = _np.fft.fftshift(_np.fft.fft(x * win))
        mag = _np.abs(spec) / n_bins
        dbm = 20.0 * _np.log10(_np.maximum(mag, 1e-12))
        dbm = _np.clip(dbm, min_dbm, max_dbm)
        return dbm.tolist()
    return _iq_to_dbm_stdlib(iq, n_bins, min_dbm, max_dbm)


def _iq_to_dbm_stdlib(iq, n_bins, min_dbm, max_dbm):
    """Pure-stdlib DFT fallback (slow; for tests / numpy-less hosts)."""
    seq = list(iq)
    if not seq:
        return [min_dbm] * n_bins
    if len(seq) != n_bins:
        step = (len(seq) - 1) / max(1, n_bins - 1)
        seq = [seq[int(round(i * step))] for i in range(n_bins)]
    N = n_bins
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1)) for i in range(N)] if N > 1 else [1.0]
    xs = [complex(seq[i]) * win[i] for i in range(N)]
    out = [0.0] * N
    for k in range(N):
        acc = 0j
        ang = -2j * math.pi * k / N
        for nidx in range(N):
            acc += xs[nidx] * complex(math.cos(ang.imag * nidx), math.sin(ang.imag * nidx))
        out[k] = abs(acc) / N
    half = N // 2                                  # fftshift: DC to centre
    out = out[half:] + out[:half]
    res = []
    for m in out:
        d = 20.0 * math.log10(m if m > 1e-12 else 1e-12)
        res.append(max(min_dbm, min(max_dbm, d)))
    return res
