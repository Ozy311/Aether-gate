#
# Aether-gate - hamlib rigctld control client (vendor-neutral CAT).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Talk to hamlib's `rigctld` daemon over its TCP text protocol.

This is the CONTROL backend for every CAT rig hamlib supports — Kenwood, Yaesu,
Elecraft, Icom-USB, ~250 models — so we never hand-port a vendor's CAT protocol
(unlike the Icom RS-BA1 LAN work). It supplies freq/mode/PTT set+read ONLY; it
carries NO spectrum or audio, so a hamlib-controlled rig must always be PAIRED
with a spectrum source (an IF-tap SoapySDR dongle, `soapy-iftap`).

rigctld is started separately (typically on the box the rig's CAT cable plugs
into), e.g.:
    rigctld -m 2014 -r /dev/ttyUSB0 -s 115200      # 2014 = Kenwood TS-2000
    rigctld -m 1035 -r /dev/ttyUSB0 -s 38400       # 1035 = Yaesu FT-991A
    rigctld -m 2 -r localhost                       # 2 = NET rigctl (chain another daemon)
and listens on TCP :4532 by default. Find model numbers with `rigctl -l`.

Protocol (rigctld "vanilla"/short-command mode): send one command line, read the
reply. Set commands return "RPRT 0" on success (negative errno on failure). Get
commands return the value line(s) then (in extended mode) "RPRT 0"; in short
mode they return just the value. We use the SHORT single-char commands and parse
the first value line, which is the most widely-compatible across hamlib versions.
Threading: one lock serialises request/reply on the socket (rigctld is not
pipelined). Stdlib only.
"""
import socket
import threading
import time

# hamlib mode strings <-> AE mode names. rigctld uses hamlib's canonical set.
HAMLIB_TO_AE = {
    "USB": "USB", "LSB": "LSB", "CW": "CW", "CWR": "CW-R", "AM": "AM",
    "FM": "FM", "WFM": "FM", "RTTY": "RTTY", "RTTYR": "RTTY-R",
    "PKTUSB": "DIGU", "PKTLSB": "DIGL", "PKTFM": "FM", "FMN": "FM-N",
}
AE_TO_HAMLIB = {
    "USB": "USB", "LSB": "LSB", "CW": "CW", "CW-R": "CWR", "AM": "AM",
    "FM": "FM", "RTTY": "RTTY", "RTTY-R": "RTTYR", "DIGU": "PKTUSB",
    "DIGL": "PKTLSB", "FM-N": "FMN",
}


class RigctldError(RuntimeError):
    pass


class Rigctld:
    """A thin, thread-safe client for a running rigctld daemon."""

    def __init__(self, host="127.0.0.1", port=4532, timeout=2.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock = None
        self._lock = threading.Lock()
        self._connected = False

    # --- connection ------------------------------------------------------
    def connect(self):
        with self._lock:
            self._connect_locked()
        return self._connected

    def _connect_locked(self):
        try:
            if self._sock:
                try: self._sock.close()
                except OSError: pass
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.settimeout(self.timeout)
            self._connected = True
        except OSError as e:
            self._connected = False
            raise RigctldError(f"rigctld connect {self.host}:{self.port} failed: {e}")

    def close(self):
        with self._lock:
            self._connected = False
            if self._sock:
                try: self._sock.close()
                except OSError: pass
                self._sock = None

    @property
    def connected(self):
        return self._connected

    # --- low-level request/reply ----------------------------------------
    def _cmd(self, line):
        """Send one command line, return the raw reply text (may be multi-line).
        Reconnects once on a broken socket. Raises RigctldError on total failure."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self._sock:
                        self._connect_locked()
                    self._sock.sendall((line + "\n").encode("ascii", "replace"))
                    return self._recv_reply_locked()
                except (OSError, RigctldError):
                    self._connected = False
                    self._sock = None
                    if attempt == 2:
                        raise RigctldError(f"rigctld command {line!r} failed")
                    time.sleep(0.1)

    def _recv_reply_locked(self):
        # rigctld replies are line-oriented; a short-mode reply is usually one
        # line (the value) or "RPRT n". Read until we have at least one full
        # line, then drain what's immediately available.
        buf = b""
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            try:
                chunk = self._sock.recv(1024)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                # got at least one line; small grace read for any trailing RPRT
                self._sock.settimeout(0.05)
                try:
                    more = self._sock.recv(1024)
                    if more:
                        buf += more
                except OSError:
                    pass
                self._sock.settimeout(self.timeout)
                break
        return buf.decode("ascii", "replace")

    @staticmethod
    def _rprt_ok(reply):
        # a set command's success line is "RPRT 0"
        for ln in reply.splitlines():
            ln = ln.strip()
            if ln.startswith("RPRT"):
                try:
                    return int(ln.split()[1]) == 0
                except (IndexError, ValueError):
                    return False
        return True   # some builds return nothing on success

    @staticmethod
    def _first_value(reply):
        for ln in reply.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("RPRT"):
                return ln
        return None

    # --- freq / mode / ptt ----------------------------------------------
    def get_freq_hz(self):
        v = self._first_value(self._cmd("f"))       # 'f' = get_freq -> "14074000"
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def set_freq_hz(self, hz):
        return self._rprt_ok(self._cmd(f"F {int(hz)}"))   # 'F' = set_freq

    def get_mode(self):
        # 'm' = get_mode -> "USB\n2400" (mode then passband)
        v = self._first_value(self._cmd("m"))
        if not v:
            return None
        return HAMLIB_TO_AE.get(v.split()[0].upper())

    def set_mode(self, ae_mode, passband=0):
        hm = AE_TO_HAMLIB.get((ae_mode or "").upper())
        if not hm:
            return False
        return self._rprt_ok(self._cmd(f"M {hm} {int(passband)}"))   # 'M' = set_mode

    def get_ptt(self):
        v = self._first_value(self._cmd("t"))       # 't' = get_ptt -> "0"/"1"
        return v == "1"

    def set_ptt(self, on):
        return self._rprt_ok(self._cmd(f"T {1 if on else 0}"))   # 'T' = set_ptt

    def get_smeter_db(self):
        # 'l STRENGTH' = get_level STRENGTH -> S-meter in dB relative to S9
        # (hamlib returns e.g. "-54" .. "+40"). None if the rig/backend lacks it.
        v = self._first_value(self._cmd("l STRENGTH"))
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def model_name(self):
        # '\dump_caps' is verbose; the short '_' returns the rig model string.
        v = self._first_value(self._cmd("_"))
        return v
