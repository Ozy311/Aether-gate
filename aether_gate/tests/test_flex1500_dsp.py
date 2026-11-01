#
# Aether-gate — FLEX-1500 host DSP tests: DC block, digital shift, honest edge,
# sideband filter (numpy; no hardware, no network).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
"""numpy-only, so these run in CI's pytest job (which installs numpy), not the
stdlib job. Synthetic IQ only — tones and constants built here; nothing stands
in for the radio's firmware.

Run:  python -m pytest aether_gate/tests/test_flex1500_dsp.py
"""
import pytest

np = pytest.importorskip("numpy")

from aether_gate.adapters.flex1500 import Flex1500Adapter, dsp, wire as w
from aether_gate.core.fft import iq_to_dbm

FS = w.SAMPLE_RATE
BLK = 768                                    # one 16-packet iso transfer


def _tone(f, n, amp=0.01, start=0):
    k = np.arange(n) + start
    return (amp * np.exp(2j * np.pi * f * k / FS)).astype(np.complex64)


def _peak_hz(iq, n_bins=None):
    """Frequency of the strongest bin as AE sees it (core iq_to_dbm, fftshifted)."""
    n_bins = n_bins or len(iq)
    db = np.asarray(iq_to_dbm(iq, n_bins, -200.0, 50.0))
    return (np.argmax(db) - n_bins // 2) * FS / n_bins, db


def _adapter(**kw):
    a = Flex1500Adapter(**kw)
    a._np = np
    return a


# --- DC block -------------------------------------------------------------

def test_dc_block_kills_constant_and_passes_300hz():
    """A constant (the measured codec DC, -147/+55 counts, Q stored negated)
    is removed to well under 1 LSB, a 300 Hz tone passes within 0.05 dB and a
    20 Hz tone within 0.6 dB. Kills: subtraction dropped (DC stays), or a
    corner far too high — at fc=300 Hz the block-mean estimate tracks the
    20 Hz tone and cancels most of it. (A 300 Hz tone alone cannot catch a high
    corner: it averages out within one 16 ms block.)"""
    dcb = dsp.DcBlocker(FS)
    dc = complex(-147, -55) / w.FULL_SCALE
    rng = np.random.default_rng(7)
    n_blocks = 200                               # 3.2 s
    tone_amp = 0.01
    out = []
    for b in range(n_blocks):
        noise = (rng.standard_normal(BLK) + 1j * rng.standard_normal(BLK)) * (0.9 / w.FULL_SCALE)
        x = (_tone(300.0, BLK, tone_amp, b * BLK) + dc + noise).astype(np.complex64)
        out.append(dcb.process(x, np))
    y = np.concatenate(out[100:])                # after the first 1.6 s
    resid = abs(np.mean(y)) * w.FULL_SCALE
    assert resid < 0.2, f"residual DC {resid:.3f} LSB"
    # tone amplitude by projection onto the 300 Hz phasor
    k = np.arange(len(y)) + 100 * BLK
    amp = abs(np.mean(y * np.exp(-2j * np.pi * 300.0 * k / FS)))
    assert abs(20 * np.log10(amp / tone_amp)) < 0.05, 20 * np.log10(amp / tone_amp)
    dcb = dsp.DcBlocker(FS)
    out = [dcb.process((_tone(20.0, BLK, tone_amp, b * BLK) + dc).astype(np.complex64), np)
           for b in range(n_blocks)]
    y = np.concatenate(out[100:])
    k = np.arange(len(y)) + 100 * BLK
    amp20 = abs(np.mean(y * np.exp(-2j * np.pi * 20.0 * k / FS)))
    assert abs(20 * np.log10(amp20 / tone_amp)) < 0.6, 20 * np.log10(amp20 / tone_amp)


def test_dc_block_is_per_i_and_q():
    """I and Q offsets are independent; both go. Kills: removing only the real
    part (or only |mean|)."""
    dcb = dsp.DcBlocker(FS)
    x = np.full(BLK, complex(0.004, -0.002), dtype=np.complex64)
    y = dcb.process(x, np)
    assert abs(y.real).max() < 1e-7 and abs(y.imag).max() < 1e-7


# --- digital shift + honest edge ------------------------------------------

def test_nco_shift_labels_tone_at_right_bin():
    """LO 10 kHz below the pan centre (USB offset). The 38.4 MHz spur 16 kHz
    above the LO must read 6 kHz above the centre through the core's FFT.
    Kills: shift sign flipped (lands at +26k -> wraps), or no shift (+16k)."""
    a = _adapter()
    center, lo = 38_394_000.0, 38_384_000.0
    raw = _tone(38_400_000.0 - lo, 4096)
    y = a._relabel(raw, lo, 0, center)
    f, _ = _peak_hz(y)
    assert abs(f - 6_000.0) <= FS / 4096, f
    # LSB side: LO above the centre, spur at 38.406 pan -> -6 kHz
    center, lo = 38_406_000.0, 38_416_000.0
    y = a._relabel(_tone(38_400_000.0 - lo, 4096), lo, 0, center)
    f, _ = _peak_hz(y)
    assert abs(f + 6_000.0) <= FS / 4096, f


def test_nco_shift_phase_continuous_across_blocks():
    """Shifting two adjacent blocks separately (with their sample indices) must
    equal shifting them joined. Kills: the NCO restarting each block (index 0)."""
    x = _tone(1234.0, 2 * BLK)
    sh = -9_937.5            # not a whole number of cycles per block (a real
                             # shift is LO-quantised, never round)
    whole = dsp.nco_shift(x, sh, 5000, FS, np)
    parts = np.concatenate([dsp.nco_shift(x[:BLK], sh, 5000, FS, np),
                            dsp.nco_shift(x[BLK:], sh, 5000 + BLK, FS, np)])
    assert np.max(np.abs(whole - parts)) < 1e-9
    # and the seam itself has no phase step
    d = np.angle(parts[1:] * np.conj(parts[:-1]))
    assert np.max(np.abs(d - d[0])) < 1e-6


def test_unsampled_edge_shows_floor_not_alias():
    """A tone the radio DID sample, 20 kHz below an LO that sits 10 kHz below
    the centre, is 30 kHz below the centre: outside the pan. Without masking it
    wraps to +18 kHz and paints a signal that is not there. It must read at the
    floor, while a real tone inside the pan still shows.
    Kills: mask_unsampled removed, or masking the wrong edge."""
    a = _adapter()
    center, lo = 14_074_000.0, 14_064_000.0
    n = 4096
    alias = _tone(-20_000.0, n, amp=0.1)
    real = _tone(5_000.0, n, amp=0.001)            # LO+5k = 5 kHz below the centre
    y = a._relabel(alias + real, lo, 0, center)
    f, db = _peak_hz(y)
    assert abs(f + 5_000.0) <= FS / n, f
    bin_18k = n // 2 + int(round(18_000.0 * n / FS))
    assert db[bin_18k] < db.max() - 100, (db[bin_18k], db.max())
    assert dsp.unsampled_band(lo - center, FS) == (14_000.0, 24_000.0)
    assert dsp.unsampled_band(+10_000.0, FS) == (-24_000.0, -14_000.0)


# --- demod: sideband filter + the offset keeps DC out of the audio ----------

def _demod_rms(a, iq, lo, blocks=8):
    out = []
    for b in range(blocks):
        out.append(a._demod(iq[b * 1536:(b + 1) * 1536], lo, 768))
    y = np.concatenate(out[2:])                  # skip filter fill
    return float(np.sqrt(np.mean(y * y)))


def _ready(a, mode="USB"):
    a._decim = 2
    a._stage_firs = []
    for M in a._factor_decim(2):
        ntaps = 4 * M + 1
        idx = np.arange(ntaps) - (ntaps - 1) / 2.0
        taps = np.sinc(2 * (0.45 / M) * idx) * np.hamming(ntaps)
        a._stage_firs.append([taps / taps.sum(), np.zeros(ntaps - 1, np.complex128), M])
    a._ssb_usb = dsp.ssb_taps(24_000, np)
    a._ssb_state = np.zeros(len(a._ssb_usb) - 1, np.complex128)
    a._mode = mode


def test_offset_keeps_dc_out_of_the_audio():
    """DC block OFF, offset ON: the codec DC sits at the LO, 10 kHz below the
    slice. In the audio it must be >= 40 dB under a passband tone of the same
    amplitude. Kills: the sideband filter removed (the DC line folds in at only
    ~5 dB down through the short decimation FIR)."""
    n = 1536 * 8
    lo, slice_hz = 14_064_000.0, 14_074_000.0
    a = _adapter(); _ready(a); a._slice_hz = slice_hz
    dc_rms = _demod_rms(a, np.full(n, 0.01, dtype=np.complex64), lo)
    b = _adapter(); _ready(b); b._slice_hz = slice_hz
    tone_rms = _demod_rms(b, _tone(slice_hz + 1_000.0 - lo, n, amp=0.01), lo)
    assert 20 * np.log10(dc_rms / tone_rms) < -40, 20 * np.log10(dc_rms / tone_rms)


def test_usb_and_lsb_are_different():
    """A tone 1 kHz above the slice is loud in USB and >= 30 dB down in LSB.
    Kills: real(conj(z)) == real(z) (both sidebands folded, USB == LSB)."""
    n = 1536 * 8
    lo, slice_hz = 14_064_000.0, 14_074_000.0
    iq = _tone(slice_hz + 1_000.0 - lo, n, amp=0.01)
    u = _adapter(); _ready(u, "USB"); u._slice_hz = slice_hz
    l = _adapter(); _ready(l, "LSB"); l._slice_hz = slice_hz
    ru, rl = _demod_rms(u, iq, lo), _demod_rms(l, iq, lo)
    assert 20 * np.log10(rl / ru) < -30, 20 * np.log10(rl / ru)


# --- ingest + S-meter follow the real LO --------------------------------------

def test_ingest_tags_lo_and_dc_block_switch():
    """Blocks carry the LO they were captured at; DC block on removes the
    offset before both paths, off leaves it (still measured for diagnostics).
    Kills: ingest ignoring dc_block, or untagged blocks."""
    dc = complex(-147, -55) / w.FULL_SCALE
    for on in (True, False):
        a = _adapter(dc_block=on)
        a.lo_hz = 14_064_000.0
        for _ in range(3):
            a._ingest(np.full(BLK, dc, dtype=np.complex64))
        lo, idx, blk = a._pan_ring[-1]
        assert lo == 14_064_000.0 and idx == 2 * BLK
        assert a._audio_q[-1][0] == 14_064_000.0
        if on:
            assert abs(blk).max() < 1e-6
        else:
            assert abs(blk.mean() - dc) < 1e-6
        i, q = a._dc_counts()
        assert abs(i + 147) < 0.5 and abs(q - 55) < 0.5, (i, q)


def test_smeter_follows_lo_offset():
    """With the DC block off and the LO 10 kHz below the slice, the S-meter
    (slice passband) must not see a carrier next to the LO; metered at the LO
    it would.
    Kills: metering at the LO instead of at slice - LO."""
    a = _adapter(dc_block=False)
    rng = np.random.default_rng(3)
    noise = ((rng.standard_normal(4096) + 1j * rng.standard_normal(4096)) / w.FULL_SCALE)
    tone = _tone(500.0, 4096, amp=0.01)          # a carrier 500 Hz above the LO
    a._pan_ring.append((14_064_000.0, 0, (noise + tone).astype(np.complex64)))
    a._slice_hz = 14_074_000.0
    off = a._s_meter_dbm()
    a._pan_ring.clear()
    a._pan_ring.append((14_064_000.0, 0, noise.astype(np.complex64)))
    quiet = a._s_meter_dbm()
    assert abs(off - quiet) < 1.0, (off, quiet)
