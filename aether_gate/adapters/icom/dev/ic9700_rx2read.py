# THE decider for true two-receiver dual-pan: can we read RX2 (SUB) without
# disrupting RX1 (MAIN) or the scope stream?
# Approach under test: select-sub (07 D1) -> read freq/mode -> select-main (07 D0).
# Measures: (a) does MAIN's freq stay put across the dance? (b) does the SCOPE
# keep flowing (frames climb) or stall when we flip receivers? (c) do we get a
# distinct SUB freq (different BAND from MAIN = real 2nd receiver)?
# Nigel: have BOTH receivers on, MAIN and SUB on DIFFERENT bands (e.g. 2m + 23cm).
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

CIV_TO_MODE = {0x00:"LSB",0x01:"USB",0x02:"AM",0x03:"CW",0x04:"RTTY",0x05:"FM",0x08:"DV",0x12:"FM-N"}


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0x0F) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


class RX2(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.sel_freq = None
        self.sel_mode = None

    def _d(self, d):
        if d.find(b"\x27\x00\x00") >= 0:
            self._on_civ(d)
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0: break
            b = d[i:end+1]
            if len(b) >= 6 and b[2] in (CONTROLLER_CIV, 0x00):
                body = b[4:-1]
                if body[0] == 0x25 and len(body) >= 6 and body[1] == 0x00:
                    self.sel_freq = unbcd(body[2:7])
                elif body[0] == 0x26 and len(body) >= 2 and body[1] == 0x00:
                    self.sel_mode = CIV_TO_MODE.get(body[2])
            i = d.find(b"\xfe\xfe", end)

    def read_sel(self, settle=0.6):
        self.sel_freq = None
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x26, 0x00]))
        time.sleep(settle)
        return self.sel_freq, self.sel_mode


def M(hz): return f"{hz/1e6:.4f}" if hz else "--"


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    r = RX2(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    r.start()
    t0 = time.time()
    while time.time() - t0 < 12 and r.sel_freq is None:
        r.read_sel()
    if r.sel_freq is None:
        print("STREAM DEAD - abort"); r.stop(); h.stop(); sys.exit(1)

    def frames(): return r.frames

    print("\n  baseline: read selected (should be MAIN)")
    f_a, m_a = r.read_sel()
    fr0 = frames(); time.sleep(1.0); fr1 = frames()
    print(f"    SELECTED = {M(f_a)} {m_a}   scope +{fr1-fr0} frames/s (baseline)")

    print("\n  >>> select SUB (07 D1), read, select MAIN back (07 D0):")
    frA = frames()
    r._send_civ(bytes([0x07, 0xD1]))          # select SUB receiver
    time.sleep(0.5)
    f_sub, m_sub = r.read_sel()
    r._send_civ(bytes([0x07, 0xD0]))          # select MAIN back
    time.sleep(0.5)
    f_main2, m_main2 = r.read_sel()
    frB = frames()
    print(f"    while SUB selected: {M(f_sub)} {m_sub}")
    print(f"    after MAIN reselected: {M(f_main2)} {m_main2}")
    print(f"    scope during dance: +{frB-frA} frames")

    print("\n  >>> scope survival after the dance (5s):")
    frC = frames()
    for k in range(5):
        time.sleep(1.0)
        print(f"    +{k+1}s frames={frames()} (+{frames()-frC})")

    print("\n  VERDICT:")
    main_stable = f_a and f_main2 and abs(f_a - f_main2) < 1000
    sub_distinct = f_sub and f_a and abs(f_sub - f_a) > 100000   # different band-ish
    scope_ok = (frames() - frC) > 10
    print(f"    MAIN stayed put across dance: {main_stable}  ({M(f_a)} -> {M(f_main2)})")
    print(f"    SUB read distinct freq:       {sub_distinct}  (SUB={M(f_sub)})")
    print(f"    scope kept flowing after:     {scope_ok}")
    if main_stable and sub_distinct and scope_ok:
        print("    => select-read-restore WORKS. Dual-receiver read is viable.")
    else:
        print("    => problems above -> select-read-restore disrupts something; rethink.")
    r.stop(); h.stop()
