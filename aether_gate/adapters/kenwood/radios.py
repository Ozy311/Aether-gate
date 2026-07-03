#
# Aether-gate - Kenwood radio registry (data-driven capability table).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Per-radio capability data for the Kenwood family the gate can bridge.

Unlike the Icom LAN rigs (one RS-BA1 stream carries control+scope+audio), Kenwood
CAT rigs split the two axes (see RADIO_SUPPORT.md):
  * CONTROL  = hamlib `rigctld` (vendor-neutral; freq/mode/PTT). No scope over CAT.
  * SPECTRUM = an IF-tap / off-air SoapySDR dongle, CAT-steered so the dongle's
               spectrum follows the rig's tuned frequency ("soapy-iftap").

So a Kenwood row carries: the hamlib model number (for `rigctld -m N`), the Flex
model to advertise to AE, the band set, and the default spectrum dongle hint. The
`hamlib_model` is the ONLY vendor-specific bit — everything else (freq/mode) rides
the generic rigctld client.

⚠ NONE hardware-verified yet, but hamlib model numbers below are CONFIRMED against
hamlib 4.5.5 (installed on linux-aether: `rigctl -l | grep -i kenwood`). Bands are
indicative widest ham allocations (region-neutral).
"""
from dataclasses import dataclass, field
from typing import List, Optional

# Reuse the Icom registry's Band shape (same concept: span + XVTR flag).
from ..icom.radios import Band, _HF, _6M, _2M, _70CM, _23CM


@dataclass
class KenwoodRadio:
    model: str                     # Kenwood model, e.g. "TS-2000"
    hamlib_model: int              # `rigctld -m N` id (rigctl -l)
    advertise: str                 # Flex model presented to AE (drives native band caps)
    bands: List[Band] = field(default_factory=list)
    # Kenwood CAT gives no scope -> spectrum comes from an IF-tap dongle.
    spectrum: str = "soapy-iftap"  # "soapy-iftap" (dongle, CAT-steered) | "audio-fft" (last resort)
    hf_dongle_needed: bool = True  # HF coverage needs an HF-capable dongle (V4/upconverter/RX888)
    verified: bool = False
    notes: str = ""

    def native_bands(self) -> List[Band]:
        return [b for b in self.bands if not b.needs_xvtr]

    def xvtr_bands(self) -> List[Band]:
        return [b for b in self.bands if b.needs_xvtr]


# Kenwood CAT rigs: control via hamlib, spectrum via IF-tap dongle.
# advertise=FLEX-6700 when the rig has 2m (unlocks AE's native 2m); FLEX-6600 for HF+6m.
REGISTRY = {
    "TS-2000": KenwoodRadio(
        model="TS-2000", hamlib_model=2014, advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM,   # (+23cm with the UT-20 option — add per-rig)
        spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m/2m/70cm multibander. hamlib -m 2014. No CAT scope -> IF-tap dongle. VERIFY."),

    "TS-450S": KenwoodRadio(
        model="TS-450S", hamlib_model=2003, advertise="FLEX-6600",
        # HF-only (160-10m incl WARC); no 60m, no 6m. Built-in CAT (no IF-10 board needed).
        bands=[b for b in _HF if b.name != "60m"],
        spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF-only 160-10m. Built-in CAT via rear serial (no IF-10 board). hamlib -m 2003 "
              "(confirmed vs 4.6.2 on Pi5). Old rig: typ 4800 baud; picky about cable. No CAT scope."),

    "TS-590SG": KenwoodRadio(
        model="TS-590SG", hamlib_model=2037, advertise="FLEX-6600",
        bands=_HF + _6M, spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m. hamlib -m 2037 (SG); TS-590S = 2031. Confirmed vs hamlib 4.5.5. No CAT scope."),

    "TS-890S": KenwoodRadio(
        model="TS-890S", hamlib_model=2041, advertise="FLEX-6600",
        bands=_HF + _6M, spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m. Built-in scope NOT over CAT -> IF-tap dongle. hamlib -m 2041 (confirmed vs 4.5.5)."),
}


def get(model: str) -> Optional[KenwoodRadio]:
    """Look up by model, tolerant of case/spaces/underscores."""
    if not model:
        return None
    key = model.strip().upper().replace("_", "-").replace(" ", "")
    for m, r in REGISTRY.items():
        if m.upper().replace("-", "").replace(" ", "") == key.replace("-", ""):
            return r
    return REGISTRY.get(model)


def supported() -> List[str]:
    return sorted(REGISTRY)
