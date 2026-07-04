#
# Aether-gate — IC-9700 LAN control-stream handler (auth state machine).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP: UdpHandler.cpp. Attribution preserved.
#
"""Control-stream auth: discovery -> login -> token -> capabilities -> conninfo,
yielding the radio-assigned civ/audio ports. Built on the threaded UdpBase so the
ping/idle/retransmit cadence runs throughout (the serial-probe bug fix)."""
import socket
import struct
import threading

from .udpbase import UdpBase
from .obfuscation import obfuscate


class Ic9700Handler(UdpBase):
    def __init__(self, local_ip, radio_ip, radio_port, username, password, name="aether-gate"):
        super().__init__(local_ip, radio_ip, radio_port, bind_port=0, name="ctrl")
        self.username = username
        self.password = password
        self.client_name = name[:16]
        self._auth_seq = 0x30
        self._tok_request = (id(self) & 0xFFFF) or 1
        self.token = 0
        self.mac = b"\x00" * 6
        self.use_guid = False
        self.civ_port = None
        self.audio_port = None
        self.civ_local_port = 0
        self.audio_local_port = 0
        self.authenticated = threading.Event()
        self.stream_ready = threading.Event()
        self.on_civ_ports = None         # callback(civ_port, audio_port)
        self.on_data = self._on_control_data

    # discovery hooks ------------------------------------------------------
    def _on_iamready(self):
        # radio acked our 0x06 -> send login (first tracked packet, seq=1)
        self._send_login()

    # auth packets ---------------------------------------------------------
    def _send_login(self):
        b = bytearray(0x80)
        struct.pack_into("<IHHII", b, 0, 0x80, 0x00, 0, self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, 0x70)
        b[0x14] = 0x01                   # requestreply = request
        b[0x15] = 0x00                   # requesttype = login
        struct.pack_into(">H", b, 0x16, self._auth_seq); self._auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)
        b[0x40:0x50] = obfuscate(self.username)
        b[0x50:0x60] = obfuscate(self.password)
        nm = self.client_name.encode("latin-1"); b[0x60:0x60 + len(nm)] = nm
        self.send_tracked(bytes(b))

    def _send_token_confirm(self):
        b = bytearray(0x40)
        struct.pack_into("<IHHII", b, 0, 0x40, 0x00, 0, self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, 0x30)
        b[0x14] = 0x01                   # request
        b[0x15] = 0x02                   # requesttype = token confirm
        struct.pack_into(">H", b, 0x16, self._auth_seq); self._auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)
        struct.pack_into("<I", b, 0x1c, self.token)
        struct.pack_into("<H", b, 0x24, 0x0798)     # resetcap
        self.send_tracked(bytes(b))

    def _send_conninfo(self):
        # reserve civ/audio local ports (bind temp sockets)
        cs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); cs.bind((self.local_ip, 0))
        as_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); as_.bind((self.local_ip, 0))
        self.civ_local_port = cs.getsockname()[1]
        self.audio_local_port = as_.getsockname()[1]
        self._civ_sock = cs; self._audio_sock = as_   # keep them bound; civ.py adopts cs
        b = bytearray(0x90)
        struct.pack_into("<IHHII", b, 0, 0x90, 0x00, 0, self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, 0x80)
        b[0x14] = 0x01                   # request
        b[0x15] = 0x03                   # requesttype = stream request
        struct.pack_into(">H", b, 0x16, self._auth_seq); self._auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)
        struct.pack_into("<I", b, 0x1c, self.token)
        if not self.use_guid:
            struct.pack_into("<H", b, 0x27, 0x8010)         # commoncap (native LE)
            b[0x2a:0x30] = self.mac
        nm = b"IC-9700"                  # devName from capabilities (set in _parse_caps)
        b[0x40:0x40 + len(self._dev_name)] = self._dev_name[:32]
        # requested_streams union @0x60: username, rxenable, txenable, codecs, samples, ports
        b[0x60:0x70] = obfuscate(self.username)
        b[0x70] = 0x01                   # rxenable
        b[0x71] = 0x00                   # txenable (RX-only first)
        b[0x72] = 0x04                   # rxcodec (LPCM16)
        b[0x73] = 0x00                   # txcodec
        struct.pack_into(">I", b, 0x74, 48000)              # rxsample
        struct.pack_into(">I", b, 0x78, 0)                  # txsample
        struct.pack_into(">I", b, 0x7c, self.civ_local_port)
        struct.pack_into(">I", b, 0x80, self.audio_local_port)
        struct.pack_into(">I", b, 0x84, 0)                  # txbuffer
        b[0x88] = 0x01                   # convert
        self.send_tracked(bytes(b))

    # control-stream replies (dispatch by exact packet length, like SDR9700) ----
    def _on_control_data(self, d):
        ln = len(d)
        if ln < 0x16:
            return

        # LOGIN_RESPONSE (0x60): read token, send token-confirm, mark authenticated
        if ln == 0x60:
            err = struct.unpack("<I", d[0x30:0x34])[0]
            if err == 0xFEFFFFFF:
                self._fail = "bad credentials"; return
            if not self.authenticated.is_set():
                self.token = struct.unpack("<I", d[0x1c:0x20])[0]
                self._send_token_confirm()
                self.authenticated.set()
            return

        # CAPABILITIES (0x42 + N*0x66; 1 radio = 0xa8): parse MAC/name, request stream
        if ln >= 0x42 + 0x66 and (ln - 0x42) % 0x66 == 0:
            if not self.stream_ready.is_set():
                self._parse_caps(d)
                self._send_conninfo()
            return

        # STATUS (0x50): the assigned civ/audio ports (big-endian)
        if ln == 0x50:
            err = struct.unpack("<I", d[0x30:0x34])[0]
            disc = d[0x40]
            if err == 0 and not disc:
                self.civ_port = struct.unpack(">H", d[0x42:0x44])[0]
                self.audio_port = struct.unpack(">H", d[0x46:0x48])[0]
                if self.civ_port and not self.stream_ready.is_set():
                    self.stream_ready.set()
                    if self.on_civ_ports:
                        self.on_civ_ports(self.civ_port, self.audio_port)
            return

    def _parse_caps(self, d):
        # first radio_cap block at 0x42; mac@0x0a within it, name@0x10 (32)
        base = 0x42
        self.mac = bytes(d[base + 0x0a:base + 0x10])
        self._dev_name = bytes(d[base + 0x10:base + 0x30]).split(b"\x00")[0]
        # commoncap is a native quint16 in the packed struct -> little-endian on the
        # LE wire (bytes 10 80 -> 0x8010). SDR9700 compares == 0x8010 to use MAC.
        cc = struct.unpack("<H", d[base + 0x07:base + 0x09])[0]
        self.use_guid = (cc != 0x8010)

    # public ---------------------------------------------------------------
    _dev_name = b"IC-9700"
    _fail = None

    def connect(self, timeout=8.0):
        """Run discovery+auth; return True when civ port is known."""
        self.start()
        return self.stream_ready.wait(timeout)
