# RF-gain discriminator: scope pixels are all zero while RF gain reads 0/255.
# Temporarily set RF gain to max, watch the scope peak + S-meter, restore.
# NOTE: Probe is imported by other dev scripts — keep the test body under
# __main__ (importing this module must not run a radio session).
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ


class Probe(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._dispatch
        self.rf_gain = None
        self.smeter = None

    def _dispatch(self, d):
        if d.find(b"\x27\x00\x00") >= 0:
            self._on_civ(d)
            return
        i = d.find(b"\xfe\xfe\xe0")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            f = bytes(d[i:end + 1])
            body = f[4:-1]
            if len(body) >= 3 and body[0] == 0x14 and body[1] == 0x02:
                self.rf_gain = (body[2] >> 4) * 1000 + (body[2] & 0xF) * 100 + \
                               (body[3] >> 4) * 10 + (body[3] & 0xF) if len(body) >= 4 else None
            if len(body) >= 3 and body[0] == 0x15 and body[1] == 0x02:
                self.smeter = (body[2] >> 4) * 1000 + (body[2] & 0xF) * 100 + \
                              (body[3] >> 4) * 10 + (body[3] & 0xF) if len(body) >= 4 else None
            i = d.find(b"\xfe\xfe\xe0", end)


def watch(civ, seconds, label):
    civ.max_byte = 0
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.25)
    civ._send_civ(bytes([0x15, 0x02]))
    time.sleep(0.4)
    dbm = f"~{-130 + min(civ.max_byte, 159) / 159 * 120:.1f} dBm"
    print(f"  [{label}] peak_raw={civ.max_byte} ({dbm})  s-meter={civ.smeter}")


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
    CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2

    h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH/PORTS FAILED:", h._fail)
        h.stop()
        sys.exit(1)

    civ = Probe(LIP, RIP, h.civ_port, h._civ_sock, CIV_ADDR)
    civ.start()
    time.sleep(1.2)

    civ._send_civ(bytes([0x14, 0x02]))           # read current RF gain
    time.sleep(0.5)
    orig = civ.rf_gain if civ.rf_gain is not None else 0
    print(f"  RF gain now: {orig}/255")

    watch(civ, 3, "RF gain as-found")

    print("setting RF gain -> 255 ...")
    civ._send_civ(bytes([0x14, 0x02, 0x02, 0x55]))   # BCD 0255
    time.sleep(0.8)
    watch(civ, 5, "RF gain max")

    print(f"restoring RF gain -> {orig} ...")
    hi, lo = divmod(orig, 100)
    civ._send_civ(bytes([0x14, 0x02, (hi // 10 << 4) | (hi % 10), (lo // 10 << 4) | (lo % 10)]))
    time.sleep(0.5)
    civ._send_civ(bytes([0x14, 0x02]))
    time.sleep(0.5)
    print(f"  RF gain reads back: {civ.rf_gain}/255")

    civ.stop()
    h.stop()
