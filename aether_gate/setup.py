#
# Aether-gate - setup / launcher web UI.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""A tiny web launcher that replaces the CLI flags: pick an adapter + radio
(from the registry), fill in the connection details, and Start/Stop the gate.

    python -m aether_gate.setup            # opens on http://<ip>:8730/

'Start' spawns `python -m aether_gate ...` as a child process with the chosen
config (so it reuses ALL the existing adapter/CLI code); 'Stop' terminates it.
Stdlib only (http.server) - no extra deps.
"""
import http.server
import json
import socket
import subprocess
import sys
import threading
import urllib.parse

from .adapters import available
from .adapters.icom import radios as icom_radios

SETUP_PORT = 8730

_proc = None            # the running gate child process
_lock = threading.Lock()
_last_argv = []


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _radios_json():
    """Registry -> JSON the page uses to auto-fill fields per radio."""
    out = {}
    for m in icom_radios.supported():
        r = icom_radios.get(m)
        out[m] = {
            "civ_addr": f"0x{r.civ_addr:02X}",
            "transport": r.transport,
            "advertise": r.advertise,
            "has_scope": r.has_scope,
            "verified": r.verified,
            "bands": [b.name for b in r.bands],
            "notes": r.notes,
        }
    return out


def _build_argv(cfg):
    """Turn the form dict into an `python -m aether_gate ...` argv."""
    a = [sys.executable, "-u", "-m", "aether_gate", "--adapter", cfg.get("adapter", "sim")]
    def add(flag, key):
        v = str(cfg.get(key, "")).strip()
        if v:
            a.extend([flag, v])
    if cfg.get("adapter") == "sim":
        add("--pattern", "pattern")
    if cfg.get("adapter") == "icom9700":
        add("--radio-ip", "radio_ip"); add("--user", "user"); add("--pass", "password")
        add("--radio-local-ip", "radio_local_ip"); add("--civ-addr", "civ_addr")
    add("--model", "model"); add("--serial", "serial"); add("--station", "station")
    add("--ip", "ip"); add("--ae", "ae"); add("--port", "port"); add("--ctl-port", "ctl_port")
    add("--fps", "fps"); add("--bins", "bins")
    return a


def _status():
    with _lock:
        running = _proc is not None and _proc.poll() is None
        return {"running": running, "pid": (_proc.pid if running else None), "argv": _last_argv}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/api/radios"):
            self._send(200, json.dumps(_radios_json()))
        elif self.path.startswith("/api/adapters"):
            self._send(200, json.dumps(available()))
        elif self.path.startswith("/api/status"):
            self._send(200, json.dumps(_status()))
        else:
            self._send(404, "{}")

    def do_POST(self):
        global _proc, _last_argv
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode() if n else "{}"
        try:
            cfg = json.loads(raw)
        except Exception:
            cfg = {}
        if self.path.startswith("/api/start"):
            with _lock:
                if _proc is not None and _proc.poll() is None:
                    self._send(409, json.dumps({"ok": False, "error": "already running"})); return
                argv = _build_argv(cfg)
                try:
                    _proc = subprocess.Popen(argv)
                    _last_argv = argv
                    self._send(200, json.dumps({"ok": True, "pid": _proc.pid, "argv": argv}))
                except Exception as e:
                    self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif self.path.startswith("/api/stop"):
            with _lock:
                if _proc is not None and _proc.poll() is None:
                    _proc.terminate()
                    try:
                        _proc.wait(timeout=5)
                    except Exception:
                        _proc.kill()
                self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, "{}")


PAGE = """<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Aether-gate - setup</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;max-width:640px;margin:0 auto;padding:20px}
 h1{color:#58a6ff;margin:0 0 2px} .sub{color:#8b949e;margin:0 0 18px;font-size:14px}
 label{display:block;margin:14px 0 4px;font-size:13px;color:#adbac7}
 input,select{width:100%;box-sizing:border-box;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px}
 .row{display:flex;gap:12px}.row>div{flex:1}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px;margin:14px 0}
 button{font-size:15px;font-weight:600;border:none;border-radius:6px;padding:10px 22px;cursor:pointer}
 #go{background:#238636;color:#fff}#stop{background:#da3633;color:#fff;margin-left:8px}
 .dot{display:inline-block;width:11px;height:11px;border-radius:50%;background:#6e7681;margin-right:8px}
 .hint{font-size:12px;color:#6e7681;margin-top:4px} #st{font-weight:600}
 .adv summary{color:#58a6ff;cursor:pointer;margin-top:10px}
 code{background:#0d1117;padding:1px 5px;border-radius:4px;color:#8b949e}
</style></head><body>
<h1>Aether-gate</h1><div class=sub>Radio setup &amp; launcher &mdash; present any radio to AetherSDR as a Flex</div>

<div class=card>
  <span class=dot id=dot></span><span id=st>checking...</span>
  <div class=hint id=argv></div>
</div>

<label>Adapter</label>
<select id=adapter onchange=onAdapter()></select>

<div id=icombox style=display:none>
  <label>Radio (Icom)</label>
  <select id=radio onchange=onRadio()></select>
  <div class=hint id=radiohint></div>
  <div class=row>
    <div><label>Radio IP</label><input id=radio_ip placeholder=10.0.0.7></div>
    <div><label>CI-V addr</label><input id=civ_addr placeholder=0xA2></div>
  </div>
  <div class=row>
    <div><label>Username</label><input id=user placeholder=nigel></div>
    <div><label>Password</label><input id=password type=password></div>
  </div>
  <label>Local IP that reaches the radio (blank = auto)</label>
  <input id=radio_local_ip placeholder="auto (e.g. 10.0.0.103)">
</div>

<div id=simbox style=display:none>
  <label>Test pattern</label>
  <select id=pattern>
   <option>test_card</option><option>carrier</option><option>two_tone</option>
   <option>ssb</option><option>cw</option><option>noise</option><option>noise_floor</option>
  </select>
</div>

<div class=card>
  <div class=row>
    <div><label>Advertise as (Flex model)</label><input id=model placeholder=FLEX-6700></div>
    <div><label>Station name (AE label)</label><input id=station placeholder=Aether-gate></div>
  </div>
  <label>Serial (unique per gate)</label><input id=serial placeholder=GATE9700>
</div>

<details class=adv><summary>Network / advanced</summary>
  <div class=row>
    <div><label>Advertise our IP</label><input id=ip placeholder=auto></div>
    <div><label>AE IP (unicast discovery)</label><input id=ae placeholder="AE's IP"></div>
  </div>
  <div class=row>
    <div><label>Port</label><input id=port value=4992></div>
    <div><label>Signal-panel port</label><input id=ctl_port value=8731></div>
  </div>
  <div class=row>
    <div><label>FPS</label><input id=fps placeholder=25></div>
    <div><label>Bins</label><input id=bins placeholder=auto></div>
  </div>
</details>

<div style=margin-top:18px>
  <button id=go onclick=start()>&#9654; Start</button>
  <button id=stop onclick=stop()>&#9632; Stop</button>
</div>
<div class=hint style=margin-top:10px>Signal panel (once started): <a style=color:#58a6ff id=panellink target=_blank>open</a></div>

<script>
let RADIOS={};
async function init(){
 const ads=await (await fetch('/api/adapters')).json();
 const as=document.getElementById('adapter'); as.innerHTML='';
 ads.forEach(a=>{const o=document.createElement('option');o.value=o.textContent=a;as.appendChild(o);});
 if(ads.includes('icom9700'))as.value='icom9700';
 RADIOS=await (await fetch('/api/radios')).json();
 const rs=document.getElementById('radio'); rs.innerHTML='';
 Object.keys(RADIOS).forEach(m=>{const o=document.createElement('option');o.value=o.textContent=m;rs.appendChild(o);});
 onAdapter(); onRadio(); poll(); setInterval(poll,2000);
}
function onAdapter(){
 const a=document.getElementById('adapter').value;
 document.getElementById('icombox').style.display=(a==='icom9700')?'block':'none';
 document.getElementById('simbox').style.display=(a==='sim')?'block':'none';
 if(a==='sim'){document.getElementById('station').placeholder='Aether-gate';document.getElementById('model').placeholder='FLEX-6600';}
}
function onRadio(){
 const m=document.getElementById('radio').value, r=RADIOS[m]; if(!r)return;
 document.getElementById('civ_addr').value=r.civ_addr;
 document.getElementById('model').value=r.advertise;
 document.getElementById('station').value='aether-gate '+m.replace('IC-','').toLowerCase();
 document.getElementById('serial').value='GATE'+m.replace('IC-','');
 document.getElementById('radiohint').innerHTML=
   'transport <b>'+r.transport+'</b> &middot; scope <b>'+(r.has_scope?'yes':'no')+'</b> &middot; bands '+r.bands.join(', ')
   +(r.verified?' &middot; <span style=color:#3fb950>verified</span>':' &middot; <span style=color:#d29922>VERIFY</span>');
}
function cfg(){
 const ids=['adapter','pattern','radio_ip','civ_addr','user','password','radio_local_ip',
            'model','station','serial','ip','ae','port','ctl_port','fps','bins'];
 const c={}; ids.forEach(i=>{const el=document.getElementById(i); if(el)c[i]=el.value;}); return c;
}
async function start(){
 const r=await (await fetch('/api/start',{method:'POST',body:JSON.stringify(cfg())})).json();
 if(!r.ok)alert('Start failed: '+(r.error||'?')); poll();
}
async function stop(){ await fetch('/api/stop',{method:'POST',body:'{}'}); poll(); }
async function poll(){
 const s=await (await fetch('/api/status')).json();
 document.getElementById('dot').style.background=s.running?'#3fb950':'#6e7681';
 document.getElementById('st').textContent=s.running?('RUNNING (pid '+s.pid+')'):'stopped';
 document.getElementById('argv').textContent=(s.argv&&s.argv.length)?s.argv.slice(3).join(' '):'';
 const cp=document.getElementById('ctl_port').value||'8731';
 document.getElementById('panellink').href='http://'+location.hostname+':'+cp+'/';
}
init();
</script></body></html>"""


def main(argv=None):
    ip = _local_ip()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", SETUP_PORT), Handler)
    print(f"Aether-gate setup UI -> http://{ip}:{SETUP_PORT}/  (and http://127.0.0.1:{SETUP_PORT}/)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with _lock:
            if _proc is not None and _proc.poll() is None:
                _proc.terminate()
        print("\nbye")


if __name__ == "__main__":
    main()
