# Cross-band tuning probe: WHY doesn't CI-V move the 9700 off 2m?
# Reads MAIN (03) + SUB (25 01), then tries in order, watching FB/FA:
#   A) 05 + BCD 435.000       (plain freq set — suspected FA cross-band)
#   B) 25 00 + BCD 435.000    (selected-VFO form, dual-receiver aware)
#   C) if SUB owns 70cm: move SUB to 23cm first (25 01), retry B
# Restores MAIN and SUB to as-found at the end.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]


def bcd(hz):
    out = bytearray()
    hz = int(hz)
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


def unbcd(b):
    f, mult = 0, 1
    for byte in b:
        f += (byte & 0x0F) * mult; mult *= 10
        f += (byte >> 4) * mult; mult *= 10
    return f


class Probe(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._dispatch
        self.log = []

    def _dispatch(self, d):
        if d.find(b"\x27\x00\x00") >= 0:
            self._on_civ(d)
            return
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            self.log.append(bytes(d[i:end + 1]))
            i = d.find(b"\xfe\xfe", end)

    def last_reply(self):
        # newest radio->us frame (fe fe e0 a2 ...)
        for f in reversed(self.log):
            if len(f) >= 5 and f[2] == 0xE0:
                return f
        return None


def send(civ, label, payload, settle=0.6):
    n0 = len(civ.log)
    civ._send_civ(payload)
    time.sleep(settle)
    replies = [f for f in civ.log[n0:] if len(f) >= 5 and f[2] == 0xE0]
    for f in replies:
        body = f[4:-1]
        if body[:1] == b"\xfb":
            print(f"  {label}: FB (OK)")
        elif body[:1] == b"\xfa":
            print(f"  {label}: FA (REFUSED)")
        else:
            print(f"  {label}: reply {f.hex()}")
    if not replies:
        print(f"  {label}: (no reply)")
    return replies


def read_freq(civ, label, cmd):
    n0 = len(civ.log)
    civ._send_civ(cmd)
    time.sleep(0.6)
    for f in civ.log[n0:]:
        body = f[4:-1]
        if len(body) >= len(cmd) + 5 and body[:len(cmd)] == cmd and f[2] == 0xE0:
            hz = unbcd(body[len(cmd):len(cmd) + 5])
            print(f"  {label}: {hz/1e6:.4f} MHz")
            return hz
    print(f"  {label}: (no parse)")
    return None


if __name__ == "__main__":
    h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)

    civ = Probe(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    civ.start()

    # wait for the CIV stream to actually come up (scope frames or any reply)
    t0 = time.time()
    while time.time() - t0 < 10 and civ.frames == 0 and not civ.log:
        time.sleep(0.5)
    print(f"  stream: frames={civ.frames} ctl-frames={len(civ.log)} after {time.time()-t0:.1f}s")
    if civ.frames == 0 and not civ.log:
        print("STREAM DEAD - aborting so the session can cool")
        civ.stop(); h.stop(); sys.exit(1)


    def read_freq_retry(civ, label, cmd, tries=4):
        for _ in range(tries):
            hz = read_freq(civ, label, cmd)
            if hz is not None:
                return hz
        return None


    main0 = read_freq_retry(civ, "MAIN (03)", bytes([0x03]))
    sub0 = read_freq_retry(civ, "SUB (25 01)", bytes([0x25, 0x01]))

    print("A) plain 05 -> 435.000 ...")
    send(civ, "05 435.000", bytes([0x05]) + bcd(435_000_000))
    after_a = read_freq(civ, "MAIN after A", bytes([0x03]))

    print("B) 25 00 -> 435.000 ...")
    send(civ, "25 00 435.000", bytes([0x25, 0x00]) + bcd(435_000_000))
    after_b = read_freq(civ, "MAIN after B", bytes([0x25, 0x00]))

    moved_sub = False
    if (after_b or after_a or 0) < 400e6 and sub0 and 420e6 < sub0 < 450e6:
        print("C) SUB owns 70cm -> move SUB to 23cm (1295.0), retry ...")
        send(civ, "SUB->1295 (25 01)", bytes([0x25, 0x01]) + bcd(1_295_000_000))
        moved_sub = True
        send(civ, "25 00 435.000 retry", bytes([0x25, 0x00]) + bcd(435_000_000))
        after_b = read_freq(civ, "MAIN after C", bytes([0x25, 0x00]))

    print("restore ...")
    if main0:
        send(civ, f"MAIN->{main0/1e6:.4f}", bytes([0x25, 0x00]) + bcd(main0))
    if moved_sub and sub0:
        send(civ, f"SUB->{sub0/1e6:.4f}", bytes([0x25, 0x01]) + bcd(sub0))
    final = read_freq(civ, "MAIN final", bytes([0x03]))

    ok = after_b and abs(after_b - 435_000_000) < 1000
    print("RESULT:", "CROSS-BAND WORKS via", ("25 00" if ok else "NOTHING TESTED — see FB/FA log above"))
    civ.stop(); h.stop()
