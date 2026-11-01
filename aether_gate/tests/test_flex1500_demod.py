#
# Aether-gate — FLEX-1500 mode-correct demod tests: passband per mode and width,
# CW pitch, AM envelope, FM discriminator, S-meter band (numpy; no hardware).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
"""numpy-only, so these run in CI's pytest job (which installs numpy), not the
stdlib job, and skip cleanly where numpy is absent. Synthetic IQ only: tones,
AM and FM signals built here and fed through the adapter's real _demod() in
engine-sized blocks (128 audio samples = 256 IQ samples at 48 kHz). Levels are
taken BEFORE the AGC, so a rejected signal reads as rejected.

The passband table itself (wire.demod_passband) is tested stdlib-only in
test_flex1500_wire.

Run:  python -m pytest aether_gate/tests/test_flex1500_demod.py
"""
import pytest

np = pytest.importorskip("numpy")

from aether_gate.adapters.flex1500 import Flex1500Adapter, wire as w

FS = w.SAMPLE_RATE                 # 48 kHz IQ
AR = 24_000                        # audio rate
N_AUDIO = 128                      # engine AUDIO_FRAMES
N_IQ = N_AUDIO * 2
BLOCKS = 96                        # ~0.5 s
SKIP = 24                          # filter fill + DC/AGC-free settling
SLICE = 7_066_600.0                # LO = slice (the default, offset 0)


def _adapter(mode="USB", width=None):
    a = Flex1500Adapter()
    a._init_demod(np)
    a._slice_hz = SLICE
    a.set_mode(mode)
    if width is not None:
        a.set_filter_width_hz(width)
    return a


def _run(a, iq):
    """Feed iq through _demod in engine blocks; the settled audio (pre-AGC)."""
    out = [a._demod(iq[b * N_IQ:(b + 1) * N_IQ], SLICE, N_AUDIO) for b in range(BLOCKS)]
    return np.concatenate(out[SKIP:])


def _tone(f, amp=0.01, n=N_IQ * BLOCKS):
    k = np.arange(n)
    return (amp * np.exp(2j * np.pi * f * k / FS)).astype(np.complex64)


def _rms(y):
    return float(np.sqrt(np.mean(y * y)) + 1e-30)


def _db(a, b):
    return 20.0 * np.log10(a / b)


def _peak_audio_hz(y):
    Y = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    return np.argmax(Y) * AR / len(y)


def _amp_at(y, f):
    """Amplitude of the f Hz component of real audio y (projection)."""
    k = np.arange(len(y))
    return 2.0 * abs(np.mean(y * np.exp(-2j * np.pi * f * k / AR)))


def _rej_db(mode, f_in, f_out, width=None):
    """dB of a tone at f_out relative to one at f_in (same amplitude), both
    relative to the slice, through `mode` at `width`."""
    yin = _run(_adapter(mode, width), _tone(f_in))
    yout = _run(_adapter(mode, width), _tone(f_out))
    return _db(_rms(yout), _rms(yin))


# --- sidebands and width ----------------------------------------------------

def test_usb_lsb_pass_their_side_reject_the_other_and_beyond_width():
    """USB passes +1 kHz; -1 kHz (wrong sideband) and +4 kHz (past the 2.8 kHz
    edge) are >= 20 dB down. LSB is the mirror.
    Kills: LSB built with the USB taps, or no high edge (a one-sided filter
    open to 10 kHz)."""
    for mode, sgn in (("USB", +1), ("LSB", -1)):
        wrong = _rej_db(mode, sgn * 1000.0, -sgn * 1000.0)
        beyond = _rej_db(mode, sgn * 1000.0, sgn * 4000.0)
        assert wrong < -20 and beyond < -20, (mode, wrong, beyond)


def test_digl_rejects_upper_digu_rejects_lower():
    """DIGL is a LOWER-sideband mode: it passes -1.5 kHz and rejects +1.5 kHz
    by >= 20 dB; DIGU the opposite.
    Kills: DIGL demodulated as upper (soapy/hpsdr's startswith("LSB"))."""
    assert _rej_db("DIGL", -1500.0, +1500.0) < -20
    assert _rej_db("DIGU", +1500.0, -1500.0) < -20


def test_width_change_narrows_the_filter():
    """A tone 2 kHz above a USB slice passes at the default 2.7 kHz width and is
    >= 20 dB down once AE sets 1.5 kHz (passband 100..1600 Hz), in the same
    adapter: the taps are rebuilt on the width change.
    Kills: set_filter_width_hz not reaching the taps (one fixed filter)."""
    a = _adapter("USB")
    tone = _tone(2000.0)
    wide = _rms(_run(a, tone))
    a.set_filter_width_hz(1500)
    narrow = _rms(_run(a, tone))
    ref = _rms(_run(_adapter("USB", 1500), _tone(1000.0)))
    assert _db(wide, ref) > -1.0, _db(wide, ref)          # passed at 2.7 kHz
    assert _db(narrow, ref) < -20, _db(narrow, ref)       # rejected at 1.5 kHz


# --- CW ----------------------------------------------------------------------

def test_cw_carrier_on_the_slice_is_heard_at_the_pitch():
    """A carrier on the slice is heard at 600 Hz in CW and CWL; 100 Hz above
    the slice it is 700 Hz in CW and 500 Hz in CWL (the BFO is on opposite
    sides).
    Kills: no BFO (zero-beat carrier inaudible), or the CWL BFO on the CW side."""
    for mode, f_rf, want in (("CW", 0.0, 600.0), ("CWL", 0.0, 600.0),
                             ("CW", 100.0, 700.0), ("CWL", 100.0, 500.0)):
        y = _run(_adapter(mode), _tone(f_rf))
        got = _peak_audio_hz(y)
        assert abs(got - want) <= 10.0, (mode, f_rf, got)


def test_cw_narrow_filter_rejects_a_signal_600hz_away():
    """At the default 500 Hz CW width (carrier +/- 250 Hz) a carrier 600 Hz
    from the slice, either side, is >= 20 dB under one on the slice.
    Kills: CW still using the 2.7 kHz SSB filter, or too few taps for a
    500 Hz filter to have skirts (63 taps: ~1.25 kHz transition)."""
    for mode in ("CW", "CWL"):
        assert _rej_db(mode, 0.0, +600.0) < -20, mode
        assert _rej_db(mode, 0.0, -600.0) < -20, mode


# --- AM ----------------------------------------------------------------------

def _am(fc, m=0.5, fmod=1000.0, amp=0.01, phase=1.3, n=N_IQ * BLOCKS):
    # carrier phase 1.3 rad: a real-part (product) detector sees cos(1.3) = 0.27
    # of the envelope, an envelope detector all of it
    k = np.arange(n)
    env = 1.0 + m * np.cos(2 * np.pi * fmod * k / FS)
    return (amp * env * np.exp(1j * (2 * np.pi * fc * k / FS + phase))).astype(np.complex64)


def test_am_envelope_recovers_1khz_and_removes_the_carrier():
    """An AM carrier on the slice, 50 % modulated at 1 kHz: the audio is the
    1 kHz tone at amp*m (the envelope), with the carrier's DC removed, and the
    same signal 6 kHz off the slice (outside +/-3 kHz) is >= 20 dB down.
    Kills: AM through the SSB product detector (real part: carrier phase
    1.3 rad leaves 27 % of the envelope), or no DC removal."""
    a = _adapter("AM")
    y = _run(a, _am(0.0))
    assert abs(_peak_audio_hz(y) - 1000.0) <= 10.0, _peak_audio_hz(y)
    amp = _amp_at(y, 1000.0)
    assert abs(_db(amp, 0.01 * 0.5)) < 1.0, _db(amp, 0.01 * 0.5)
    assert abs(np.mean(y)) < 0.05 * amp, np.mean(y)
    off = _amp_at(_run(_adapter("AM"), _am(6000.0)), 1000.0)
    assert _db(off, amp) < -20, _db(off, amp)


# --- FM ----------------------------------------------------------------------

def _fm(fc, dev=2500.0, fmod=1000.0, amp=0.1, n=N_IQ * BLOCKS):
    k = np.arange(n)
    ph = 2 * np.pi * fc * k / FS + (dev / fmod) * np.sin(2 * np.pi * fmod * k / FS)
    return (amp * np.exp(1j * ph)).astype(np.complex64)


def _noise(sigma, seed, n=N_IQ * BLOCKS):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * sigma).astype(np.complex64)


def test_fm_discriminator_recovers_1khz_deviation_tone():
    """NBFM, 2.5 kHz deviation at 1 kHz: the audio is the 1 kHz tone at the
    discriminator's scale (2*dev/fs_audio = 0.208, soapy's divide-by-pi).
    Kills: FM through the SSB detector, or a discriminator that loses the
    previous sample across blocks (a tick every block)."""
    y = _run(_adapter("FM"), _fm(0.0) + _noise(1e-4, 1))
    assert abs(_peak_audio_hz(y) - 1000.0) <= 10.0, _peak_audio_hz(y)
    amp = _amp_at(y, 1000.0)
    assert abs(amp - 2 * 2500.0 / AR) < 0.1 * (2 * 2500.0 / AR), amp
    k = np.arange(len(y))
    ph = np.exp(2j * np.pi * 1000.0 * k / AR)
    c = 2.0 * np.mean(y * np.conj(ph))                # fitted 1 kHz phasor
    resid = y - np.real(c * ph)
    assert _rms(resid) < 0.1 * _rms(y), (_rms(resid), _rms(y))   # it IS the tone
    # no tick at the 128-sample block boundaries (a discriminator that forgets
    # the previous block's last sample outputs 0 there: a spike up to 0.2)
    assert np.max(np.abs(resid)) < 0.03, np.max(np.abs(resid))


def test_fm_rejects_a_signal_outside_the_channel():
    """At an 8 kHz FM width (+/-4 kHz) an FM signal 9 kHz off the slice
    (1 kHz deviation, so 8..10 kHz) is rejected: its 1 kHz tone is >= 20 dB
    under the in-channel one over the same noise floor. (The discriminator is
    amplitude-blind, so the floor is what the leaked signal must lose to.)
    Kills: the FM channel filter ignoring the width / missing."""
    sig_in = _run(_adapter("FM", 8000), _fm(0.0, dev=1000.0) + _noise(1e-3, 2))
    sig_out = _run(_adapter("FM", 8000), _fm(9000.0, dev=1000.0) + _noise(1e-3, 2))
    a_in, a_out = _amp_at(sig_in, 1000.0), _amp_at(sig_out, 1000.0)
    assert _db(a_out, a_in) < -20, _db(a_out, a_in)


# --- taps rebuilt on change only; S-meter on the active passband ---------------

def test_taps_rebuilt_only_when_mode_or_width_changes():
    """Many blocks, per-frame set_mode()/set_filter_width_hz() repeats: one
    build. A real width or mode change: one more each.
    Kills: rebuilding the filter per block (and resetting its history, a tick
    every 5.33 ms)."""
    a = _adapter("USB")
    tone = _tone(1000.0)
    for b in range(40):
        a.set_mode("USB")
        a.set_filter_width_hz(2700)
        a._demod(tone[b * N_IQ:(b + 1) * N_IQ], SLICE, N_AUDIO)
    assert a.taps_builds == 1, a.taps_builds
    a.set_filter_width_hz(1800)
    a._demod(tone[:N_IQ], SLICE, N_AUDIO)
    a._demod(tone[:N_IQ], SLICE, N_AUDIO)
    assert a.taps_builds == 2, a.taps_builds
    a.set_mode("CW")
    a._demod(tone[:N_IQ], SLICE, N_AUDIO)
    assert a.taps_builds == 3, a.taps_builds


def test_smeter_measures_the_active_passband():
    """A carrier 1 kHz below the slice reads in LSB and not in USB; one 1 kHz
    above reads in USB and not in CW (500 Hz); one 2 kHz off reads in AM.
    Kills: the meter on a fixed 0..3 kHz band whatever the mode and width."""
    rng = np.random.default_rng(5)
    n = 4096
    noise = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) / w.FULL_SCALE)

    def meter(mode, f):
        a = Flex1500Adapter(dc_block=False)
        a._np = np
        a._slice_hz = SLICE
        a.set_mode(mode)
        k = np.arange(n)
        x = noise + 0.01 * np.exp(2j * np.pi * f * k / FS)
        a._pan_ring.append((SLICE, 0, x.astype(np.complex64)))
        return a._s_meter_dbm()

    def quiet(mode):
        a = Flex1500Adapter(dc_block=False)
        a._np = np
        a._slice_hz = SLICE
        a.set_mode(mode)
        a._pan_ring.append((SLICE, 0, noise.astype(np.complex64)))
        return a._s_meter_dbm()

    assert meter("LSB", -1000.0) > quiet("LSB") + 20
    assert abs(meter("USB", -1000.0) - quiet("USB")) < 1.0
    assert meter("USB", +1000.0) > quiet("USB") + 20
    assert abs(meter("CW", +1000.0) - quiet("CW")) < 1.0
    assert meter("AM", -2000.0) > quiet("AM") + 20
