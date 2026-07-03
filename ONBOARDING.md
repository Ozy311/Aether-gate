# Aether-gate — onboarding & packaging (design)

**Status:** design · 2026-07-03 · G0JKN. Goal: **someone picks this up and gets a radio on
AetherSDR without being a Python developer.** The plumbing works (Icom LAN, Kenwood via hamlib,
SoapySDR dongles); the *day-one experience* is the gap this note addresses.

## The day-one target

> Install once → launch → a **setup page opens in the browser** → pick your radio, fill a couple
> of fields → **Start** → it appears in AetherSDR. Next launch → it reconnects on its own.

No CLI flags, no hand-built Python env.

## What exists today

- **Radio Setup launcher** (`python -m aether_gate.setup`, web UI on `:8730`) — pick adapter + radio
  from the registry, fill fields, Start/Stop (spawns the gate). Good foundation.
- **CLI** (`python -m aether_gate --adapter … --radio-ip … …`) — the power path.
- **systemd services** on the Pi5 appliance.

## Gaps for a newcomer (in priority order)

1. **Bare launch did the wrong thing.** `python -m aether_gate` defaulted to the *sim* adapter, and
   the setup UI was a *separate* command a newcomer wouldn't know to run. **→ FIXED (this note's
   commit): bare `python -m aether_gate` (or `--setup`) now opens the Setup UI and pops the browser.**
2. **Environment/install is the real wall.** A ham won't assemble `numpy` + **SoapySDR (+ Python
   bindings)** + **hamlib** by hand. The config screen is fine; *getting a working Python* is the
   barrier. **This is the #1 unsolved item — needs packaging (below).**
3. **No persistent config / profiles.** The launcher builds CLI args fresh each Start; nothing is
   saved. Need saved radio profiles + "connect to last/default on startup".
4. **No auto-detect.** The user types IPs / picks models by hand. The gate could *discover*: scan the
   LAN for Icom RS-BA1 radios, enumerate SoapySDR devices, list serial ports → "found IC-9700 at
   10.0.0.7, connect?".
5. **Launcher is Icom-only + no dependency guidance** (the radio-family-tabs TODO): Kenwood/Yaesu
   aren't pickable in the UI yet, and it doesn't tell the user "this rig needs hamlib — not installed".

## Design

### Startup / first-run flow
- **Bare launch → Setup UI + browser** (done). On a headless box, `--no-browser` just prints the URL.
- **Saved profiles:** the Setup page writes a small `config.json` (radio, adapter, connection fields,
  identity). A **"Start on launch"** toggle per profile.
- **Smart startup:** on launch, if a profile is marked auto-start → start it (and still serve the
  Setup page for changes); else → show the Setup page. (This is the SmartSDR "connect to last radio"
  parity, but gate-side.)

### Packaging (the big lever — pick one to lead)
| Route | Fit | Effort | Notes |
|---|---|---|---|
| **Pi image** (SD-card appliance) | ⭐ best fit — matches how it already runs (Pi5 services) | med | Flash, boot, browse to `:8730`. Bundles SoapySDR + hamlib + dongle drivers pre-built. The "download and go" story. |
| **Docker image** | good for Linux/NAS users | low-med | `docker run` with USB/host-net; bundles all deps. Cross-distro. |
| **One-file executable** (PyInstaller) | Windows/Mac desktop hams | med-high | Hardest for SoapySDR/hamlib native deps; fine for LAN-only Icom (no native libs). |
Recommendation: **Pi image first** (strongest, matches the appliance model), Docker second.

### Dependency handling (make it honest)
Deps are **per-adapter**, so the Setup UI should check + guide:
- `sim` / `icom9700` (LAN) → **stdlib + numpy only** (no native libs — the easy path).
- `soapy` / any dongle / IF-tap spectrum → **SoapySDR + driver + Python binding**.
- `kenwood` / `yaesu` / CAT → **hamlib (`rigctld`)** on PATH.
Setup UI: per chosen radio, show required deps + present/absent + a one-liner to install. A packaged
build makes this moot for that platform.

### Auto-detect / discovery (pre-fill the Setup page)
- Icom LAN: send the RS-BA1 "are-you-there" broadcast → list responders (IP + model).
- SoapySDR: `Device.enumerate()` → list dongles.
- Serial: list `/dev/ttyUSB*` / COM ports for CAT rigs.
Turn manual entry into "pick the radio we found".

## Roadmap
1. ✅ **Bare launch → Setup UI + browser** (this commit).
2. **Saved profiles + start-on-launch** (persistent `config.json`).
3. **Packaging: Pi image** (bundle deps; flash-and-go).
4. **Auto-detect** (LAN Icom / SoapySDR / serial) to pre-fill Setup.
5. **Family tabs + dependency checks** in the Setup UI (Icom/Kenwood/Yaesu/dongle).
6. Docker + (LAN-only) one-file exe as secondary distributions.

## Note for maintainers (the *code* pickup)
Separate from the end-user story: a dev cloning the repo needs a short **README "Getting started"**
(deps per adapter, how to run the sim, how to run against a real radio) + this doc + `RADIO_SUPPORT.md`
(architecture) + `adapters/ICOM9700_PLAN.md`. The registry (`adapters/icom/radios.py`,
`adapters/kenwood/radios.py`) is the "add a radio = one data row" contract.
