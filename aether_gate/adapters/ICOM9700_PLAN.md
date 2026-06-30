# IC-9700 LAN adapter — design + build plan (Aether-gate)

**Status:** design / pre-implementation · 2026-06-30 · radio currently UNPOWERED (needs a
power cable) so this is the build plan to execute once it's live.
**License:** SDR9700 (github.com/w5jwp/SDR9700) is GPL-3.0; Aether-gate is GPL-3.0 on
release → direct reuse permitted **with attribution + GPL headers preserved**. The
behavioral spec on the NAS (`_claude/references/ic9700-lan-protocol-reference-2026-06-27.md`)
is the map; SDR9700 `src/radio/` is the reference code.

## Where it fits the adapter contract

The IC-9700 sends **pre-computed scope bytes** (CI-V 27h), not IQ — so this is a
**`provides="spectrum"`** adapter (like sim, NOT like soapy). The byte→dBm conversion is a
one-liner; the core already turns dBm bins into VITA-49. So the EASY 10% is the spectrum
seam; the HARD 90% is the Icom LAN transport (handshake + reliability) that gets the bytes
off the radio.

```
IC-9700 ──UDP(control/civ/audio)──► [Icom-LAN transport: handshake + reliability]
                                          │ CI-V 27h scope frames
                                          ▼
                                   scope bytes 0..159 ──► dBm bins ──► RadioAdapter.get_spectrum()
                                          │
                                          ▼  (existing core, unchanged)
                                   VITA-49 FFT/waterfall ──► AE draws the 9700 as a "Flex"
```

## New module layout (under aether_gate/)

```
adapters/
  icom9700.py        # the RadioAdapter (provides="spectrum"); get_spectrum() returns latest scope dBm
  icom/              # the Icom-LAN transport (ported from SDR9700, GPL-3.0 headers)
    __init__.py
    packets.py       # struct pack/unpack for every packet (formats below)
    obfuscation.py   # the 256-byte SEQUENCE table + obfuscate() (firmware constant)
    udpbase.py       # reliability layer: seq nums, TX history, RX-missing, retransmit, timers
    handler.py       # control-stream handshake state machine (discovery→login→token→caps→status→conninfo)
    civ.py           # CI-V stream: openclose, scope-frame parse (27 00 00 ... FD), byte→dBm
    # audio.py       # LATER (Opus/PCM RTP) — NOT in the RX-pan milestone
```

## Wire facts (from the agent's SDR9700 read — verbatim, port these)

- **Common header (all packets), little-endian:** `<IHHI2I` = len(u32), type(u16), seq(u16),
  sentid(u32), rcvdid(u32). 16 bytes. ⚠️ Several INNER fields are **big-endian** (payloadsize,
  innerseq, civport, audioport, commoncap, sample rates) — mixed-endian, watch carefully.
- **Session id:** `myId = (octet3<<24)|(octet4<<16)|(localPort&0xffff)` from local IP+port.
- **Handshake order:** are-you-there(type 0x03)→I-am-here(0x04)→I-am-ready(0x06) → LOGIN(0x80B,
  requesttype 0x00, obfuscated user/pass @0x40/0x50) → LOGIN_RESPONSE(0x60B, token@0x1c,
  error 0xFEFFFFFF = bad creds) → TOKEN confirm(requesttype 0x02) → CAPABILITIES(numradios@0x40
  BE, 0x66B per radio) → CONNINFO/request-stream(0x90B, requesttype 0x03, rxenable=1 txenable=0
  for RX-only) → STATUS(0x50B, **civport@0x42 BE**, audioport@0x46 BE).
- **Ports:** control = fixed (default 50001, but agent skeleton used 50000 — VERIFY against the
  radio's Network menu); civ + audio ports are **assigned by the radio** in the STATUS packet.
- **CI-V stream:** new UDP socket → are-you-there → `sendOpenClose(magic=0x04)` to start →
  watchdog re-opens if no RX >2 s.
- **Scope frame in a CI-V data packet:** header `reply=0xc1`@0x10, datalen@0x11 LE, sendseq@0x13
  BE, then CI-V frame at 0x15. Scope payload = pattern **`27 00 00` … `FD`**; **~490 scope bytes**
  per sweep (confirm on hardware — could differ by span/edge-mode).
- **byte→dBm:** `dbm = -130 + (min(b,159)/159)*120` (min −130, max −10 default). Trivial.
- **Obfuscation:** 256-byte SEQUENCE table (agent captured it) + per-char
  `p=(byte+i)%127; if p>126: p=32+(p%127); out=TABLE[p]`. It's a firmware constant — reuse with
  attribution.
- **Timers (ms):** ping 500, idle 100, are-you-there 500, watchdog 500, retransmit 100,
  token-renewal 60000. Use a MONOTONIC clock (flex-sim/our code already does).
- **Reliability:** per-stream u16 seq (wraps 0x10000, clears TX buffer on wrap); TX history
  ≤500 entries / 10 s; RX-missing tracker asks retransmit (type 0x01) up to 4×.

## Build milestones (each ends in a runnable, testable proof)

**M0 — offline packet codec (do FIRST, testable with NO radio).**
`packets.py` + `obfuscation.py` with round-trip unit tests: pack→unpack identity for every
struct; obfuscate a known string and assert against a captured expected (or against SDR9700
run locally). This is the only part fully testable while the radio's off — knock it out now if
desired.

**M1 — connect + authenticate (needs radio live).**
`udpbase.py` + `handler.py`: discovery→login→token→caps→status, reach "streamOpened", print the
assigned civ/audio ports. Success = clean auth + ports, token renewing every 60 s, no timeout.
Risk: obfuscation table / mixed-endian / session-id — debug with wireshark vs a known-good
SDR9700 capture.

**M2 — CI-V scope → dBm (the payoff).**
`civ.py`: open the CI-V stream, subscribe 27h scope, parse `27 00 00 … FD`, byte→dBm. Log bin
count + a min/max/median each sweep. Success = a sane live dBm array that changes with real RF.

**M3 — wire into the gate (RX-only panadapter — THE milestone).**
`icom9700.py` RadioAdapter(provides="spectrum"): a background thread runs M1+M2, keeps the
latest dBm sweep; `get_spectrum(ctx,t)` resamples it to `ctx.n` bins. Run
`python -m aether_gate --adapter icom9700 --radio-ip ... --user ... --pass ...`; AE discovers
"aether-gate 2" (FLEX), draws the **9700's 2 m/70 cm/23 cm waterfall**. Smallest end-to-end
proof; no audio, no TX.

**M4+ (later):** VFO/mode CI-V (tune the real 9700 from AE), then audio (Opus decode → the gate's
audio plane — needs `opuslib` on the Pi, NOT installed yet), then TX. Each its own session.

## Open questions to resolve on the live radio
1. **Control port** — 50001 (spec) vs 50000 (agent skeleton). Check the 9700 Network menu.
2. **Scope byte count** — 490 assumed; confirm, and whether 9700 uses split-waterfall (11×50)
   by default (the parser must handle both — single-sweep first).
3. **Credentials** — the 9700's Network username/password (set in its menu); needed for login.
4. **Edge vs centre scope mode** — affects how scope bins map to the pan's freq axis (the gate
   needs the centre + span to place bins; CI-V 27h subcommands set the scope range).

## License hygiene (do at M0)
- Put a GPL-3.0 header + "Ported from github.com/w5jwp/SDR9700 (GPL-3.0), © its authors" on
  every file in `adapters/icom/`. The obfuscation table cites the firmware/SDR9700 provenance.
- Reminder: distributing a BUILT gate or repo access to anyone = the GPL-publication moment;
  fine while private + Nigel-only.

## Reference pointers
- Behavioral spec: NAS `_claude/references/ic9700-lan-protocol-reference-2026-06-27.md`
- Reference code: linux-aether `/srv/build/SDR9700/src/{radio,backend}/` (builds clean)
- TODO: get the **Icom IC-9700 CI-V Reference Guide PDF** (27h subcommands + full CAT) → NAS
  `_claude/datasheets/` — needed for M4 (VFO/mode) and to confirm scope-range subcommands.
```
