# Dual-receiver STATE probe — the foundation read for the full MAIN/SUB model.
# Reads, once a second, side by side:
#   03       -> MAIN vfo freq (legacy read)
#   25 00    -> SELECTED vfo freq
#   25 01    -> UNSELECTED (other) vfo freq
#   26 00    -> SELECTED vfo mode+filter+data (if supported)
# Prints only when the picture changes. Nigel drives the rig across bands
# (2m -> 440 -> 23cm) + swaps MAIN/SUB, and we learn EXACTLY what each read
# reports on each band — including whether 23cm reads back at all, and on
# which receiver. Read-only; sets nothing.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

CIV_TO_MODE = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
               0x05: "FM", 0x06: "CW-R", 0x07: "RTTY-R", 0x08: "DV", 0x12: "FM-N"}


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0x0F) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


def fmt(hz):
    return f"{hz/1e6:9.4f}" if hz else "    --   "


class State(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.f_main = None      # 03
        self.f_sel = None       # 25 00
        self.f_unsel = None     # 25 01
        self.mode_sel = None    # 26 00

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
                    self.f_main = unbcd(data[:5])
                elif cmd == 0x25 and len(data) >= 6:
                    if data[0] == 0x00:
                        self.f_sel = unbcd(data[1:6])
                    elif data[0] == 0x01:
                        self.f_unsel = unbcd(data[1:6])
                elif cmd == 0x26 and len(data) >= 2 and data[0] == 0x00:
                    self.mode_sel = CIV_TO_MODE.get(data[1], f"0x{data[1]:02x}")
            i = d.find(b"\xfe\xfe", end)

    def poll(self):
        self._send_civ(bytes([0x03]))
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x25, 0x01]))
        self._send_civ(bytes([0x26, 0x00]))


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    st = State(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    st.start()

    # wait for the stream, prove commands land (reply to our read)
    t0 = time.time()
    while time.time() - t0 < 12 and st.f_main is None and st.f_sel is None:
        st.poll(); time.sleep(1.0)
    if st.f_main is None and st.f_sel is None:
        print("STREAM DEAD / deaf session - abort (wait 40s, retry)")
        st.stop(); h.stop(); sys.exit(1)

    print()
    print("  ##################################################")
    print("  #  GO — move the rig now: 2m -> 440 -> 23cm       #")
    print("  #  (+ [MAIN/SUB] swap if you like). Watching 60s. #")
    print("  ##################################################")
    print("   t  MAIN(03)  SEL(25 00)  OTHER(25 01)  mode(26 00)")
    print("  " + "-" * 55)
    last = None
    for k in range(60):
        st.poll()
        time.sleep(1.0)
        cur = (st.f_main, st.f_sel, st.f_unsel, st.mode_sel)
        if cur != last:
            print(f"  {k:2d} {fmt(st.f_main)} {fmt(st.f_sel)}  {fmt(st.f_unsel)}   {st.mode_sel}", flush=True)
            last = cur
    print()
    print("  READINGS: which read tracked 23cm? did 25 01 (other vfo) ever populate?")
    print("  did a [MAIN/SUB] swap move SEL<->OTHER? -> that's the state model.")
    st.stop(); h.stop()
