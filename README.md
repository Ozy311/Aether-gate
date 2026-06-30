# Aether-gate
**AetherSDR for any radio**

A universal radio abstraction layer — connects any SDR hardware, transceiver, or
remote WebSDR to AetherSDR via the FlexRadio protocol. AE keeps exactly one ingest
boundary; the zoo of sources lives outside it (see [DESIGN.md](DESIGN.md)).

*Private — pre-release.*

## Status

Core scaffold in place. The source-agnostic Flex-protocol engine (vendored from
flex-sim, AE-validated) is split from the signal source behind a `RadioAdapter`
contract. A reference `sim` adapter drives AE with the built-in test patterns, no
hardware. Real adapters (SoapySDR/RTL-SDR first) are next.

```
aether_gate/
  core/        Flex-protocol engine (engine.py, vendored) + iq_to_dbm (fft.py)
  adapters/    RadioAdapter contract (base.py), registry, sim adapter
  tests/       offline smoke tests
  __main__.py  CLI: python -m aether_gate --adapter sim
```

## Run (sim adapter — no hardware)

```bash
# Same host as AE: use a non-4992 control port so they don't clash.
python -m aether_gate --adapter sim --pattern test_card --port 5992 --ae <AE-ip>
# Separate box (its own IP, binds 4992 like a real radio):
python -m aether_gate --adapter sim --pattern carrier --ae <AE-ip>
```

## Test

```bash
python -m aether_gate.tests.test_smoke
```

## Writing an adapter

Subclass `aether_gate.adapters.base.RadioAdapter`, set `provides`, implement one
source method, and `register()` it:

- `provides = "spectrum"` → implement `get_spectrum(ctx, t) -> [dBm]*ctx.n`
- `provides = "iq"` → implement `get_iq(n, center_hz, span_hz) -> complex samples`
  (the core runs the FFT)

Plus optional `open/close`, `retune(center_hz)`, `set_mode(mode)`, `read_meters()`.
See `adapters/sim.py` for the reference.
