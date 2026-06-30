#
# Aether-gate — SimAdapter: the built-in test-pattern source (reference adapter).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""SimAdapter — wraps the vendored flex-sim pattern engine as a spectrum adapter.

This is the reference implementation of the RadioAdapter contract and the proof
that the core+adapter split works: it drives AE with the exact, AE-validated test
patterns flex-sim already produces, with zero hardware. Every real adapter
(SoapySDR, CI-V, ...) implements the same contract this one does.
"""
from ..core.engine import PATTERNS, PatternCtx  # noqa: F401 (PatternCtx re-exported for typing)
from .base import RadioAdapter, AdapterCaps


class SimAdapter(RadioAdapter):
    """A spectrum adapter backed by flex-sim's pat_* generators."""

    provides = "spectrum"

    def __init__(self, pattern="carrier", model="FLEX-6600", serial=""):
        if pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {pattern!r}; choose from {sorted(PATTERNS)}")
        self.pattern = pattern
        self.capabilities = AdapterCaps(model=model, serial=serial, tx_capable=True)

    def get_spectrum(self, ctx, t):
        return PATTERNS[self.pattern](ctx, t)

    def wants_tx(self, ctx):
        # Mirror flex-sim's pattern-driven TX: tx_blank/cw assert real TX.
        if self.pattern == "cw":
            return ctx.cw_in_message
        return None
