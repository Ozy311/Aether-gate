# RX1=1.2GHz, RX2=146.985 are set. WHICH CI-V read returns 146.985?
# Fires many candidate reads and prints every distinct freq each returns, so
# we find the command that actually reaches the 2nd RECEIVER (not just VFO A/B
# of RX1). Also tries select-then-read with several select commands.
# Read-only-ish (selects are restored to main at the end). No scope needed.
import sys, time
sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

def unbcd(b):
    f,m=0,1
    for x in b:
        f+=(x&0xF)*m; m*=10
        f+=(x>>4)*m; m*=10
    return f
def M(hz): return f"{hz/1e6:.4f}" if hz else "--"

class Find(Ic9700Civ):
    def __init__(self,*a,**k):
        super().__init__(*a,**k); self.on_data=self._d
        self.last={}   # (cmd,sub) -> freq
    def _d(self,d):
        if d.find(b"\x27\x00\x00")>=0: self._on_civ(d)
        i=d.find(b"\xfe\xfe")
        while i>=0:
            e=d.find(b"\xfd",i)
            if e<0: break
            b=d[i:e+1]
            if len(b)>=6 and b[2] in (CONTROLLER_CIV,0x00):
                body=b[4:-1]
                # 03 (main freq), 25 00/01 (sel/unsel vfo freq)
                if body[0]==0x03 and len(body)>=6:
                    self.last[('03',None)]=unbcd(body[1:6])
                elif body[0]==0x25 and len(body)>=6:
                    self.last[('2500' if body[1]==0 else '2501',None)]=unbcd(body[2:7])
            i=d.find(b"\xfe\xfe",e)
    def rd(self,label,payload,settle=0.5):
        self._send_civ(payload); time.sleep(settle)

if __name__=="__main__":
    RIP,RPORT,USER,PASS,LIP=sys.argv[1:6]
    h=Ic9700Handler(LIP,RIP,int(RPORT),USER,PASS)
    print("auth...");
    if not h.connect(timeout=9.0): print("AUTH FAIL",h._fail); h.stop(); sys.exit(1)
    r=Find(LIP,RIP,h.civ_port,h._civ_sock,0xA2); r.start()
    t0=time.time()
    while time.time()-t0<12 and not r.last:
        r.rd("03",bytes([0x03])); r.rd("2500",bytes([0x25,0x00]))
    if not r.last: print("DEAD"); r.stop(); h.stop(); sys.exit(1)

    print("\n=== A) plain reads (no select) — expect RX1=1.2G on all ===")
    for lbl,pl in [("03(main freq)",bytes([0x03])),("25 00(sel vfo)",bytes([0x25,0x00])),("25 01(unsel vfo)",bytes([0x25,0x01]))]:
        r.last.clear(); r.rd(lbl,pl,0.7)
        vals={k:M(v) for k,v in r.last.items()}
        print(f"   {lbl:18s} -> {vals}")

    print("\n=== B) select MAIN/SUB via 07 D0/D1, then read 25 00 ===")
    for lbl,sel in [("07 D0 (main)",0xD0),("07 D1 (sub)",0xD1)]:
        r._send_civ(bytes([0x07,sel])); time.sleep(0.5)
        r.last.clear(); r.rd("25 00 after "+lbl,bytes([0x25,0x00]),0.7)
        print(f"   after {lbl:14s} -> 25 00 = {M(r.last.get(('2500',None)))}")

    print("\n=== C) select VFO-band-swap 07 B0, read; then swap back ===")
    r._send_civ(bytes([0x07,0xB0])); time.sleep(0.6)
    r.last.clear(); r.rd("25 00 after 07 B0",bytes([0x25,0x00]),0.7)
    print(f"   after 07 B0 (swap main/sub) -> 25 00 = {M(r.last.get(('2500',None)))}")
    r._send_civ(bytes([0x07,0xB0])); time.sleep(0.6)   # swap back

    print("\n=== D) 07 D2 (band main/sub toggle) variants ===")
    for val in (0x00,0x01):
        r._send_civ(bytes([0x07,0xD2,val])); time.sleep(0.5)
        r.last.clear(); r.rd(f"25 00 after 07 D2 {val:02x}",bytes([0x25,0x00]),0.6)
        print(f"   after 07 D2 {val:02x} -> 25 00 = {M(r.last.get(('2500',None)))}")

    r._send_civ(bytes([0x07,0xD0])); time.sleep(0.4)   # restore main
    print("\n  LOOKING FOR: which read/select returned 146.985 (=RX2/2m).")
    print("  Whichever did = the way to reach the 2nd RECEIVER.")
    r.stop(); h.stop()
