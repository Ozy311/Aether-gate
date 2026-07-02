# Which CI-V read reports "SUB receiver active"? The 07 D2 (dualwatch) flag
# read False while Nigel had 2m+23cm dual-receive running. Icom distinguishes
# DUALWATCH (2 rx same band) from MAIN/SUB dual-receive (2 bands) from SAT mode.
# Poll several candidates + watch 25 01 (does the OTHER vfo read a real freq?)
# — the reliable "SUB is live" signal may simply be "25 01 differs from 25 00".
#   07 D2  dualwatch on/off
#   07 D0/D1 are SELECTS not reads; skip
#   16 5C  ? (some Icoms: dualwatch)   16 5F ? — try a couple 16-group reads
#   25 01  OTHER vfo freq  (if it reads a plausible freq, SUB exists)
#   1A 05 ...  menu items (skip — model-specific)
# Nigel: have 2m + 23cm (or any 2 bands) BOTH running, leave it. Read-only.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0x0F) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


class DW(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.reads = {}     # label -> raw hex payload of last reply
        self.f_sel = None
        self.f_oth = None

    def _d(self, d):
        if d.find(b"\x27\x00\x00") >= 0:
            self._on_civ(d)
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            f = d[i:end + 1]
            if len(f) >= 6 and f[2] in (CONTROLLER_CIV, 0x00):
                body = f[4:-1]              # cmd + subcmd + data
                cmd = body[0]
                if cmd == 0x25 and len(body) >= 6:
                    if body[1] == 0x00:
                        self.f_sel = unbcd(body[2:7])
                    elif body[1] == 0x01:
                        self.f_oth = unbcd(body[2:7])
                elif cmd == 0x07 and len(body) >= 2 and body[1] == 0xD2:
                    self.reads["07 D2 (dualwatch)"] = body[2:].hex()
                elif cmd == 0x16 and len(body) >= 2:
                    self.reads[f"16 {body[1]:02X}"] = body[2:].hex()
            i = d.find(b"\xfe\xfe", end)

    def poll(self):
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x25, 0x01]))
        self._send_civ(bytes([0x07, 0xD2]))
        for sub in (0x5C, 0x5F, 0x59):     # candidate 16-group dualwatch-ish reads
            self._send_civ(bytes([0x16, sub]))


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    d = DW(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    d.start()
    t0 = time.time()
    while time.time() - t0 < 12 and d.f_sel is None:
        d.poll(); time.sleep(1.0)
    if d.f_sel is None:
        print("STREAM DEAD - abort"); d.stop(); h.stop(); sys.exit(1)

    print("\n  reading for 6s (have BOTH receivers running)...")
    for _ in range(6):
        d.poll(); time.sleep(1.0)

    print(f"\n  SEL (25 00) = {d.f_sel/1e6 if d.f_sel else None} MHz")
    print(f"  OTHER (25 01) = {d.f_oth/1e6 if d.f_oth else None} MHz")
    print(f"  -> OTHER reads a real freq: {bool(d.f_oth and d.f_oth > 1e6)}  "
          f"(differs from SEL: {d.f_oth != d.f_sel})")
    print("\n  candidate flag reads (payload hex):")
    for k, v in sorted(d.reads.items()):
        print(f"    {k:22s} = {v}")
    print("\n  KEY: 07 D2 = the dualwatch byte. If it's 00 while both rx run,")
    print("       dualwatch != dual-receive on this rig -> use 'OTHER reads a")
    print("       distinct real freq' as the 'SUB active' signal instead.")
    d.stop(); h.stop()
