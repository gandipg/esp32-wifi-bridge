"""
wifi_setup.py — ESP32 Serial Bridge v5
HTTP server + WiFi setup.
Reset and baud changes are executed via callbacks
(actual execution happens in uart_bridge_thread, not here).
"""

import network, socket, json, machine, utime, _thread

AP_SSID = "ESP32-Setup"
AP_PASS = "12345678"
CONFIG  = "config.json"
CHUNK   = 2048

_ap = _sta = None
_uart = _pin_rst = _pin_boot = _baud = None
_reset_cb = None    # fn(type_str) — set by set_hardware
_baud_cb  = None    # fn(key, val)

def set_hardware(uart, pin_rst, pin_boot, baud, reset_cb=None, baud_cb=None):
    global _uart, _pin_rst, _pin_boot, _baud, _reset_cb, _baud_cb
    _uart     = uart
    _pin_rst  = pin_rst
    _pin_boot = pin_boot
    _baud     = baud
    _reset_cb = reset_cb
    _baud_cb  = baud_cb

# ── WiFi ──────────────────────────────────────────────────

def get_sta():
    global _sta
    if _sta is None: _sta = network.WLAN(network.STA_IF)
    return _sta

def get_ap():
    global _ap
    if _ap is None: _ap = network.WLAN(network.AP_IF)
    return _ap

def try_connect(ssid, pw, timeout=15):
    print("[WIFI] connecting:", ssid)
    sta = get_sta(); sta.active(True); sta.connect(ssid, pw)
    for _ in range(timeout):
        if sta.isconnected():
            ip = sta.ifconfig()[0]; print("[WIFI] OK:", ip); return ip
        utime.sleep(1)
    print("[WIFI] failed"); return None

def scan_networks():
    sta = get_sta(); sta.active(True)
    try: nets = sta.scan()
    except: return []
    seen = {}
    for n in nets:
        try: ssid = n[0].decode("utf-8","ignore").strip()
        except: continue
        if ssid and ssid not in seen: seen[ssid] = n[3]
    return sorted(seen.items(), key=lambda x:-x[1])

def start_ap():
    ap = get_ap(); ap.active(True)
    ap.config(essid=AP_SSID, password=AP_PASS, authmode=3)
    utime.sleep_ms(300); print("[WIFI] AP 192.168.4.1")

def stop_ap():
    get_ap().active(False)

# ── Config file ───────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG) as f: return json.load(f)
    except: return {"ssid":"","pass":""}

def save_config(ssid, pw):
    with open(CONFIG,"w") as f: json.dump({"ssid":ssid,"pass":pw},f)

# ── HTTP helpers ──────────────────────────────────────────

def url_decode(s):
    s = s.replace("+"," "); out = ""; i = 0
    while i < len(s):
        if s[i]=="%" and i+2<len(s):
            try: out+=chr(int(s[i+1:i+3],16)); i+=3; continue
            except: pass
        out+=s[i]; i+=1
    return out

def parse_form(body):
    p = {}
    for part in body.split("&"):
        if "=" in part:
            k,v=part.split("=",1); p[url_decode(k)]=url_decode(v)
    return p

def recv_req(conn):
    conn.settimeout(5); raw = b""
    try:
        while b"\r\n\r\n" not in raw:
            c = conn.recv(512)
            if not c: break
            raw += c
            if len(raw) > 8192: break
    except: pass
    method, path = "GET", "/"
    try:
        fl = raw.split(b"\r\n")[0].decode("utf-8","ignore").split()
        if len(fl)>=2: method=fl[0]; path=fl[1].split("?")[0]
    except: pass
    body = b""
    for line in raw.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                cl = int(line.split(b":")[1].strip())
                ex = raw.split(b"\r\n\r\n",1)
                body = ex[1] if len(ex)>1 else b""
                conn.settimeout(3)
                while len(body)<cl:
                    m=conn.recv(cl-len(body))
                    if not m: break
                    body+=m
            except: pass
            break
    return method, path, body

def send_json(conn, data, status=200):
    if not isinstance(data,(str,bytes)): data=json.dumps(data)
    if isinstance(data,str): data=data.encode()
    st={200:"OK",400:"Bad Request",404:"Not Found"}.get(status,"OK")
    h=("HTTP/1.1 {} {}\r\nContent-Type: application/json\r\n"
       "Content-Length: {}\r\nConnection: close\r\n\r\n").format(status,st,len(data))
    try: conn.sendall(h.encode()); conn.sendall(data)
    except: pass

def send_file(conn, fname):
    try:
        import os; size=os.stat(fname)[6]
    except: size=0
    h=("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
       "Content-Length: {}\r\nConnection: close\r\n\r\n").format(size)
    try:
        conn.sendall(h.encode())
        with open(fname,"rb") as f:
            while True:
                chunk=f.read(CHUNK)
                if not chunk: break
                conn.sendall(chunk)
    except Exception as e: print("[WEB] file err:",e)

def send_404(conn):
    try: conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length:9\r\nConnection:close\r\n\r\nNot found")
    except: pass

def send_redirect(conn, url):
    h="HTTP/1.1 303 See Other\r\nLocation:{}\r\nContent-Length:0\r\nConnection:close\r\n\r\n".format(url)
    try: conn.sendall(h.encode())
    except: pass

# ── Web server ────────────────────────────────────────────

VALID_BAUDS = {9600,19200,38400,57600,115200,230400,460800,921600}

def web_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 80))
    srv.listen(3); srv.settimeout(1)
    print("[WEB] :80 ready")

    while True:
        try:   conn,addr=srv.accept()
        except OSError: continue
        except Exception as e: print("[WEB] accept:",e); continue

        closed=False
        try:
            method,path,body=recv_req(conn)
            print("[WEB]",method,path)

            if path in ("/",""):
                send_redirect(conn,"/status.html")
            elif path in ("/status.html","/config.html","/terminal.html"):
                send_file(conn,path[1:])
            elif path=="/api/status":
                sta=get_sta(); ap=get_ap()
                wip=sta.ifconfig()[0] if sta.isconnected() else None
                send_json(conn,{
                    "ip":   wip or "192.168.4.1",
                    "wifi": bool(wip),
                    "ssid": sta.config("essid") if sta.isconnected() else "",
                    "ap":   ap.active(),
                    "baud": dict(_baud) if _baud else {}
                })
            elif path=="/scan":
                nets=scan_networks()
                send_json(conn,[{"ssid":s,"rssi":r} for s,r in nets])
            elif path=="/connect" and method=="POST":
                p=parse_form(body.decode("utf-8","ignore"))
                ssid=p.get("ssid","").strip(); pw=p.get("pass","").strip()
                if not ssid:
                    send_json(conn,{"ok":False})
                else:
                    send_json(conn,{"ok":True})
                    conn.close(); closed=True
                    save_config(ssid,pw)
                    def _bg(s,pw): try_connect(s,pw)
                    _thread.start_new_thread(_bg,(ssid,pw))
            elif path=="/stopap"  and method=="POST": stop_ap();  send_json(conn,{"ok":True})
            elif path=="/startap" and method=="POST": start_ap(); send_json(conn,{"ok":True})

            elif path=="/reset" and method=="POST":
                p=parse_form(body.decode("utf-8","ignore"))
                rtype=p.get("type","normal")
                if _reset_cb:
                    _reset_cb(rtype)
                    send_json(conn,{"ok":True,"type":rtype})
                else:
                    # fallback — direct pin toggle (no callback set yet)
                    if _pin_rst:
                        _pin_rst.value(0); utime.sleep_ms(100); _pin_rst.value(1)
                    send_json(conn,{"ok":True,"type":"normal"})

            elif path=="/baud" and method=="POST":
                p=parse_form(body.decode("utf-8","ignore"))
                changed={}; error=None
                for key in ("esp","avr","dbg"):
                    if key in p and _baud is not None:
                        try:
                            v=int(p[key])
                            if v not in VALID_BAUDS: error="invalid: "+str(v); break
                            _baud[key]=v
                            if _baud_cb: _baud_cb(key,v)
                            changed[key]=v
                        except: error="bad number"
                if error: send_json(conn,{"ok":False,"error":error},400)
                else:     send_json(conn,{"ok":True,"changed":changed})

            elif path=="/reboot" and method=="POST":
                send_json(conn,{"ok":True})
                conn.close(); closed=True
                if _reset_cb: _reset_cb("micropython")
                else: utime.sleep_ms(200); machine.reset()

            else:
                send_404(conn)

        except Exception as e:
            print("[WEB] err:",e)
        finally:
            if not closed:
                try: conn.close()
                except: pass

def connect_or_setup():
    cfg=load_config(); ssid=cfg.get("ssid",""); ip=None
    if ssid: ip=try_connect(ssid,cfg.get("pass",""))
    if not ip:
        print("[SETUP] AP mode"); start_ap(); ip="192.168.4.1"
    _thread.start_new_thread(web_server,())
    return ip
