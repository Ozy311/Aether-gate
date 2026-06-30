import sys, time
sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler

RIP,RPORT,USER,PASS,LIP = sys.argv[1],int(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5]
h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
def ports(c,a): print(f"  CIV PORTS: civ={c} audio={a}")
h.on_civ_ports = ports
print("connecting...")
ok = h.connect(timeout=9.0)
print(f"  authenticated event: {h.authenticated.is_set()}")
print(f"  stream_ready (civ port known): {ok}")
print(f"  fail: {h._fail}")
print(f"  token: 0x{h.token:08x}  civ_port: {h.civ_port}  audio_port: {h.audio_port}")
print("RESULT:", "AUTH + STREAM PORTS OK" if ok else ("AUTH OK but no ports" if h.authenticated.is_set() else "FAILED"))
h.stop()
