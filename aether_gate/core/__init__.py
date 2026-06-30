#
# Aether-gate — core engine package.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The source-agnostic Flex-protocol core.

engine.py is vendored from flex-sim (nigelfenton/flex-sim, GPL-3.0), live-validated
against AetherSDR, with one seam added: Radio accepts an optional `adapter` that
supplies the per-frame spectrum (see adapters/). fft.py is the core-side IQ->dBm
transform shared by all IQ adapters.
"""
from .engine import Radio, Rack, PatternCtx, PATTERNS, MODELS, local_ip
from .fft import iq_to_dbm

__all__ = ["Radio", "Rack", "PatternCtx", "PATTERNS", "MODELS", "local_ip", "iq_to_dbm"]
