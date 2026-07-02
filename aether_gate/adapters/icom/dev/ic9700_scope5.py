# End-to-end scope proof: 45s watch window on 146.52 while a real signal
# (HT key-up) is present.  Preamp ON + RF gain max during the window; both
# restored after.  Prints a line every 2s so key-up timing is visible.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.dev.ic9700_scope4 import Probe  # reuse dispatcher

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2
WINDOW_S = 45

h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print("auth...")
if not h.connect(timeout=9.0):
    print("AUTH/PORTS FAILED:", h._fail)
    h.stop()
    sys.exit(1)

civ = Probe(LIP, RIP, h.civ_port, h._civ_sock, CIV_ADDR)
civ.start()
time.sleep(1.2)

civ._send_civ(bytes([0x14, 0x02]))
time.sleep(0.4)
orig_gain = civ.rf_gain if civ.rf_gain is not None else 0

print("preamp ON + RF gain max for the window ...")
civ._send_civ(bytes([0x16, 0x02, 0x01]))
civ._send_civ(bytes([0x14, 0x02, 0x02, 0x55]))
time.sleep(0.5)

print(f"WATCHING {WINDOW_S}s - key the HT on 146.520 now (a couple of 5s bursts)")
t0 = time.time()
best = 0
while time.time() - t0 < WINDOW_S:
    time.sleep(2.0)
    civ._send_civ(bytes([0x15, 0x02]))
    time.sleep(0.3)
    pk = civ.max_byte
    if civ.latest_dbm:
        nonzero = sum(1 for v in civ.latest_dbm if v > -130.0)
    else:
        nonzero = 0
    print(f"  +{time.time()-t0:4.1f}s  peak_raw={pk:3d}  nonzero_bins={nonzero:3d}  s-meter={civ.smeter}")
    best = max(best, pk)

print("restoring preamp OFF + RF gain ...")
civ._send_civ(bytes([0x16, 0x02, 0x00]))
hi, lo = divmod(orig_gain, 100)
civ._send_civ(bytes([0x14, 0x02, (hi // 10 << 4) | (hi % 10), (lo // 10 << 4) | (lo % 10)]))
time.sleep(0.5)

if best > 10:
    print(f"RESULT: SCOPE PATH PROVEN - peak_raw hit {best} "
          f"(~{-130 + min(best,159)/159*120:.0f} dBm). Zeros were just a quiet band.")
elif best > 0:
    print(f"RESULT: pixels moved (peak {best}) - path works, level low.")
else:
    print("RESULT: still zero WITH a live carrier - waveform output is broken; "
          "next step = SDR9700 wire capture diff.")

civ.stop()
h.stop()
