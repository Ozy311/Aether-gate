# Does the selected-VFO read (25 00) track a BAND change made at the rig?
# Reads 03 (MAIN) and 25 00 (selected) side by side, once a second, for 40s.
# Nigel changes the 9700 to 440 (and back) during the window; we watch which
# read follows.  Read-only — never sets anything.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV


def unbcd(b):
    f, mult = 0, 1
    for x in b:
        f += (x & 0x0F) * mult; mult *= 10
        f += (x >> 4) * mult; mult *= 10
    return f


class Watch(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.f03 = None      # MAIN (03)
        self.f25 = None      # selected VFO (25 00)

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
                cmd, data = f[4], f[5:-1]
                if cmd in (0x00, 0x03) and len(data) >= 5:
                    self.f03 = unbcd(data[:5])
                elif cmd == 0x25 and len(data) >= 6 and data[0] == 0x00:
                    self.f25 = unbcd(data[1:6])
            i = d.find(b"\xfe\xfe", end)


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    w = Watch(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    w.start()
    t0 = time.time()
    while time.time() - t0 < 10 and w.f03 is None and w.f25 is None:
        w._send_civ(bytes([0x03])); w._send_civ(bytes([0x25, 0x00]))
        time.sleep(1.0)
    if w.f03 is None and w.f25 is None:
        print("STREAM DEAD - abort"); w.stop(); h.stop(); sys.exit(1)

    print(">>> CHANGE THE 9700 TO 440 NOW (then back to 2m). Watching 40s...")
    last = None
    for _ in range(40):
        w._send_civ(bytes([0x03])); w._send_civ(bytes([0x25, 0x00]))
        time.sleep(1.0)
        cur = (w.f03, w.f25)
        if cur != last:
            m03 = f"{w.f03/1e6:.4f}" if w.f03 else "?"
            m25 = f"{w.f25/1e6:.4f}" if w.f25 else "?"
            print(f"  03(MAIN)={m03}  25 00(SEL)={m25}")
            last = cur
    print("done - which column followed the band change tells us the right poll")
    w.stop(); h.stop()
