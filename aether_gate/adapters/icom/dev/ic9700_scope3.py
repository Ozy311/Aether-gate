# Read back the radio's own view of its scope + RX state, in one session.
# Logs every non-waveform CI-V frame in arrival order so command/reply pairs
# and FB/FA correlations are visible.  Read-only except one freq-set retry
# (162.550 NOAA) which is restored.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2


def bcd_freq(hz):
    out = bytearray()
    hz = int(hz)
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


class Probe(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._dispatch
        self.log = []

    def _dispatch(self, d):
        if d.find(b"\x27\x00\x00") >= 0:
            self._on_civ(d)          # waveform: keep peak tracking
            return
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            f = bytes(d[i:end + 1])
            self.log.append((time.time(), f.hex()))
            i = d.find(b"\xfe\xfe", end)


h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print("auth...")
if not h.connect(timeout=9.0):
    print("AUTH/PORTS FAILED:", h._fail)
    h.stop()
    sys.exit(1)

civ = Probe(LIP, RIP, h.civ_port, h._civ_sock, CIV_ADDR)
civ.start()
time.sleep(1.2)

READS = [
    ("freq",        bytes([0x03])),
    ("mode",        bytes([0x04])),
    ("scope on?",   bytes([0x27, 0x10])),
    ("output?",     bytes([0x27, 0x11])),
    ("main/sub?",   bytes([0x27, 0x12])),
    ("scope mode?", bytes([0x27, 0x14, 0x00])),
    ("span?",       bytes([0x27, 0x15, 0x00])),
    ("hold?",       bytes([0x27, 0x17, 0x00])),
    ("ref?",        bytes([0x27, 0x19, 0x00])),
    ("speed?",      bytes([0x27, 0x1A, 0x00])),
    ("during-tx?",  bytes([0x27, 0x1B])),
    ("preamp?",     bytes([0x16, 0x02])),
    ("att?",        bytes([0x11])),
    ("rf gain?",    bytes([0x14, 0x02])),
    ("s-meter?",    bytes([0x15, 0x02])),
]
for label, cmd in READS:
    civ._send_civ(cmd)
    time.sleep(0.25)

print("freq-set retry: 162.550 ...")
mark = len(civ.log)
civ._send_civ(bytes([0x05]) + bcd_freq(162_550_000))
time.sleep(1.0)

print("restore 146.520 ...")
civ._send_civ(bytes([0x05]) + bcd_freq(146_520_000))
time.sleep(1.0)

print(f"\n--- all non-waveform frames ({len(civ.log)}) ---")
t0 = civ.log[0][0] if civ.log else 0
for t, f in civ.log:
    print(f"  +{t - t0:5.2f}s  {f}")
print(f"\nscope frames={civ.frames} peak_raw={civ.max_byte}")

civ.stop()
h.stop()
