import sys, struct
sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
RIP,RPORT,USER,PASS,LIP=sys.argv[1],int(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5]
h=Ic9700Handler(LIP,RIP,RPORT,USER,PASS)
# wrap _send_conninfo to dump our bytes
orig_send=h.send_tracked
def wrap(buf):
    if len(buf)==0x90: print(f"  MY CONNINFO 0x90: {bytes(buf).hex()}")
    return orig_send(buf)
h.send_tracked=wrap
od=h._on_control_data
def tr(d):
    if len(d)==0x50:
        err=struct.unpack("<I",d[0x30:0x34])[0]; civ=struct.unpack(">H",d[0x42:0x44])[0]
        print(f"  STATUS after: err=0x{err:08x} civ={civ}")
    od(d)
h._on_control_data=tr; h.on_data=tr
h.connect(timeout=9.0)
print(f"  civ={h.civ_port} use_guid={h.use_guid} mac={h.mac.hex()}")
h.stop()
