import sys, time, struct
sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler

RIP,RPORT,USER,PASS,LIP = sys.argv[1],int(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5]
h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
orig = h._on_control_data
def traced(d):
    ln = struct.unpack("<I", d[0:4])[0]; rr=d[0x14] if len(d)>0x15 else 0; rt=d[0x15] if len(d)>0x15 else 0
    print(f"  CTRL reply len=0x{ln:x} rr=0x{rr:02x} rt=0x{rt:02x}")
    orig(d)
h._on_control_data = traced; h.on_data = traced
print("connecting...")
ok = h.connect(timeout=9.0)
print(f"  auth={h.authenticated.is_set()} stream={ok} token=0x{h.token:08x} civ={h.civ_port} fail={h._fail}")
h.stop()
