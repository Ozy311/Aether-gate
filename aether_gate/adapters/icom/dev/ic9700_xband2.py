# Prove the cross-band recipe: 25 00 direct; on FA -> 07 B0 (swap main/sub)
# -> 25 00 again.  Expectation with SUB parked on 70cm:
#   MAIN->23cm direct: FB (23cm unheld)   |   MAIN->70cm direct: FA
#   swap, then ->70cm: FB
# Restores MAIN to as-found (SUB may end on a different 70cm freq — its
# original can't be read while hidden; noted, harmless).
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.dev.ic9700_xband import Probe, bcd, unbcd, send, read_freq

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]

if __name__ == "__main__":
    h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    civ = Probe(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    civ.start()
    t0 = time.time()
    while time.time() - t0 < 10 and civ.frames == 0 and not civ.log:
        time.sleep(0.5)
    if civ.frames == 0 and not civ.log:
        print("STREAM DEAD - abort"); civ.stop(); h.stop(); sys.exit(1)

    main0 = None
    for _ in range(4):
        main0 = read_freq(civ, "MAIN as-found", bytes([0x03]))
        if main0: break
    if not main0:
        print("cannot read MAIN - abort"); civ.stop(); h.stop(); sys.exit(1)

    print("1) MAIN -> 23cm (1295.0) direct ...")
    send(civ, "25 00 1295.0", bytes([0x25, 0x00]) + bcd(1_295_000_000))
    m = read_freq(civ, "MAIN now", bytes([0x03]))

    print("2) restore MAIN ...")
    send(civ, "25 00 back", bytes([0x25, 0x00]) + bcd(main0))
    read_freq(civ, "MAIN now", bytes([0x03]))

    print("3) MAIN -> 70cm (435.0) direct (expect FA) ...")
    send(civ, "25 00 435.0", bytes([0x25, 0x00]) + bcd(435_000_000))
    m = read_freq(civ, "MAIN now", bytes([0x03]))

    if m and abs(m - 435_000_000) > 1000:
        print("4) swap main/sub (07 B0) then 435.0 ...")
        send(civ, "07 B0 swap", bytes([0x07, 0xB0]))
        time.sleep(0.5)
        read_freq(civ, "MAIN after swap", bytes([0x03]))
        send(civ, "25 00 435.0", bytes([0x25, 0x00]) + bcd(435_000_000))
        m = read_freq(civ, "MAIN now", bytes([0x03]))

    print("5) restore: swap back + retune ...")
    send(civ, "07 B0 swap back", bytes([0x07, 0xB0]))
    time.sleep(0.5)
    send(civ, "25 00 main0", bytes([0x25, 0x00]) + bcd(main0))
    read_freq(civ, "MAIN final", bytes([0x03]))

    ok = m and abs(m - 435_000_000) < 1000
    print("RESULT:", "RECIPE PROVEN - swap unlocks 70cm" if ok else "recipe incomplete - see log")
    civ.stop(); h.stop()
