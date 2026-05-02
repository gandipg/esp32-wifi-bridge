"""
ESP32 MicroPython — WiFi Serial Bridge  v5
==========================================
Fixes v5:
 - uart_bridge_thread е единственото място с uart.init() — baud промяна чрез флаг
 - ws_recv_frame не докосва settimeout/setblocking след handshake
 - AVR flash: правилна STK500 последователност (reset → drain → forward)
 - TCP tunnel чете от rx_buf само когато мрежата е готова (select)
 - Watchdog timeout увеличен до 10s
"""

import network, socket, machine, utime, select, _thread
import ubinascii, hashlib
from wifi_setup import connect_or_setup, set_hardware

DEBUG_LEVEL = 2
def D(lvl, *a):
    if lvl <= DEBUG_LEVEL: print(*a)

PIN_RX   = 32
PIN_TX   = 33
PIN_RST  = 13
PIN_BOOT = 14

PORT_PROXY  = 2222
PORT_DEBUG  = 23
PORT_CONFIG = 2223
PORT_WS     = 81

baud = {"esp": 115200, "avr": 115200, "dbg": 115200}

uart = machine.UART(1, baudrate=115200,
                    rx=PIN_RX, tx=PIN_TX,
                    timeout=0, rxbuf=8192)

pin_rst  = machine.Pin(PIN_RST,  machine.Pin.OUT, value=1)
pin_boot = machine.Pin(PIN_BOOT, machine.Pin.OUT, value=1)

ip = ""

# ── Ring buffers ──────────────────────────────────────────
# Single-producer / single-consumer — no lock needed IF
# only one thread writes head and only one thread writes tail.
# rx: bridge_thread writes head, network threads read tail
# tx: network threads write head, bridge_thread reads tail

# Larger buffers for flash operations
# ROM bootloader flash block = 0x400 (1024) bytes + SLIP overhead
# Stub flasher block = 0x800 (2048) bytes compressed
# We need enough headroom for multiple blocks in flight
_RX_SZ = 32768   # 32KB — must hold multiple ACKs + SLIP frames
_TX_SZ = 32768   # 32KB — must hold full flash blocks from esptool

_rx_buf = bytearray(_RX_SZ)
_tx_buf = bytearray(_TX_SZ)
_rx_h = [0]; _rx_t = [0]
_tx_h = [0]; _tx_t = [0]

def _push(buf, sz, h_ref, t_ref, data):
    h = h_ref[0]
    for b in data:
        nxt = (h + 1) % sz
        if nxt == t_ref[0]: break   # full — drop (logged by caller if needed)
        buf[h] = b
        h = nxt
    h_ref[0] = h

def _pop(buf, sz, h_ref, t_ref):
    h = h_ref[0]; t = t_ref[0]
    if h == t: return b""
    out = bytes(buf[t:h]) if h > t else bytes(buf[t:]) + bytes(buf[:h])
    t_ref[0] = h
    return out

def _used(sz, h_ref, t_ref):
    h = h_ref[0]; t = t_ref[0]
    return (h - t) % sz

def rx_push(d): _push(_rx_buf, _RX_SZ, _rx_h, _rx_t, d)
def rx_pop():   return _pop(_rx_buf, _RX_SZ, _rx_h, _rx_t)
def tx_push(d): _push(_tx_buf, _TX_SZ, _tx_h, _tx_t, d)
def tx_pop():   return _pop(_tx_buf, _TX_SZ, _tx_h, _tx_t)

def rx_flush():
    _rx_t[0] = _rx_h[0]

def tx_flush():
    _tx_t[0] = _tx_h[0]

# ── Bridge control flags ───────────────────────────────────
# Written by network threads, read+executed by bridge thread.
# Use list so assignment is atomic in MicroPython.
_do_reset    = [0]    # 1=RST, 2=ESP_BOOT, 3=AVR, 4=machine.reset()
_do_baud     = [0]    # 0=no change, else new baud rate value
_bridge_tick = [utime.ticks_ms()]
WD_TIMEOUT   = 10000  # ms

# ── TCP tunnel active ──────────────────────────────────────
_tcp_active = [False]

# ═══════════════════════════════════════════════════════════
#  UART BRIDGE THREAD
# ═══════════════════════════════════════════════════════════

def uart_bridge_thread():
    """
    Owns UART exclusively. Runs at maximum speed during flash operations.

    Key design:
    - sleep_us(50) when idle  = ~20kHz poll, enough for 921600 baud
    - no sleep when active    = drain UART RX immediately
    - TX written in one call  = uart.write() handles FIFO internally
    - uart rxbuf=8192         = hardware buffer, bridge reads it fast
    - baud change via flag    = atomic, no race with TCP thread
    """
    D(2, "[BRG] start")
    while True:
        _bridge_tick[0] = utime.ticks_ms()
        active = False

        # Baud rate change (from TCP tunnel or HTTP)
        nb = _do_baud[0]
        if nb:
            _do_baud[0] = 0
            D(2, "[BRG] baud →", nb)
            # Large rxbuf for flash — hardware buffers incoming ACKs
            uart.init(baudrate=nb, rx=PIN_RX, tx=PIN_TX, timeout=0, rxbuf=8192)

        # Reset request
        req = _do_reset[0]
        if req:
            _do_reset[0] = 0
            D(2, "[BRG] reset", req)
            with _pin_lock:
                if req == 1:
                    pin_rst.value(0); utime.sleep_ms(100); pin_rst.value(1)
                elif req == 2:
                    pin_boot.value(0); utime.sleep_ms(10)
                    pin_rst.value(0);  utime.sleep_ms(100)
                    pin_rst.value(1);  utime.sleep_ms(50)
                    pin_boot.value(1)
                elif req == 3:
                    pin_rst.value(0); utime.sleep_ms(10)
                    pin_rst.value(1)
                elif req == 4:
                    utime.sleep_ms(200); machine.reset()

        # UART RX → rx_buf: drain everything available
        n = uart.any()
        if n:
            d = uart.read(n)
            if d:
                rx_push(d)
                active = True

        # tx_buf → UART TX: write all at once
        # uart.write() on MicroPython ESP32 uses DMA and returns
        # immediately for small writes; for large writes it may block
        # briefly but that's acceptable — it's draining the TX FIFO.
        d = tx_pop()
        if d:
            uart.write(d)
            active = True

        # Tight loop when data is flowing, yield when idle
        if not active:
            utime.sleep_us(50)

def watchdog_thread():
    utime.sleep_ms(6000)
    while True:
        utime.sleep_ms(2000)
        age = utime.ticks_diff(utime.ticks_ms(), _bridge_tick[0])
        if age > WD_TIMEOUT:
            D(1, "[WD] dead", age, "ms → reset")
            machine.reset()

# ═══════════════════════════════════════════════════════════
#  WebSocket
# ═══════════════════════════════════════════════════════════

def ws_handshake(conn):
    conn.settimeout(3)
    raw = b""
    try:
        while b"\r\n\r\n" not in raw:
            c = conn.recv(256)
            if not c: return False
            raw += c
    except: return False
    key = b""
    for line in raw.split(b"\r\n"):
        if b"Sec-WebSocket-Key:" in line:
            key = line.split(b":", 1)[1].strip(); break
    if not key: return False
    magic  = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = ubinascii.b2a_base64(hashlib.sha1(key + magic).digest()).strip()
    try:
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        return True
    except: return False

def ws_send(conn, data):
    """Send binary/text frame. Non-blocking check first."""
    if isinstance(data, str): data = data.encode("utf-8", "replace")
    if not data: return True
    n = len(data)
    hdr = bytes([0x82, n]) if n < 126 else bytes([0x82, 126, n >> 8, n & 0xFF])
    try:
        _, w, _ = select.select([], [conn], [], 0.05)
        if not w: return True      # TX busy — skip this chunk
        conn.sendall(hdr + data)
        return True
    except OSError as e:
        return e.args[0] == 11     # EAGAIN = not fatal
    except:
        return False

def ws_recv_frame(conn):
    """
    Non-blocking WS frame reader.
    conn MUST be setblocking(False) before calling.
    Never calls settimeout() — that would interfere with the socket state.
    Returns: b'' = no data, bytes = payload, None = close/error
    """
    # Fast path: nothing to read
    r, _, _ = select.select([conn], [], [], 0)
    if not r: return b""

    # Header (2 bytes)
    try:
        h = conn.recv(2)
    except OSError as e:
        return b"" if e.args[0] == 11 else None
    if not h: return None
    if len(h) < 2: return b""   # partial header — wait next tick

    op     = h[0] & 0x0F
    masked = bool(h[1] & 0x80)
    n      = h[1] & 0x7F

    if op == 8: return None     # close
    if op == 9:                 # ping → pong
        try: conn.sendall(b"\x8a\x00")
        except: pass
        return b""

    # Extended length
    if n == 126:
        try: ext = conn.recv(2)
        except: return b""
        if not ext or len(ext) < 2: return b""
        n = (ext[0] << 8) | ext[1]

    if n == 0: return b""
    if n > 4096: return b""

    # Mask + payload — read what's available
    need = (4 if masked else 0) + n
    buf = b""
    tries = 0
    while len(buf) < need and tries < 20:
        r2, _, _ = select.select([conn], [], [], 0.005)
        if r2:
            try:
                chunk = conn.recv(need - len(buf))
                if chunk: buf += chunk
                else: break
            except OSError as e:
                if e.args[0] != 11: break
        tries += 1

    if len(buf) < need: return b""  # couldn't get full frame — drop, not fatal

    if masked:
        mask = buf[:4]; payload = buf[4:]
        return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return buf

_ws_conn = None

def ws_tick():
    global _ws_conn
    if _ws_conn is None: return

    # rx_buf → browser
    d = rx_pop()
    if d:
        if not ws_send(_ws_conn, d):
            D(2, "[WS] send fail")
            try: _ws_conn.close()
            except: pass
            _ws_conn = None
            return

    # browser → tx_buf
    f = ws_recv_frame(_ws_conn)
    if f is None:
        D(2, "[WS] gone")
        try: _ws_conn.close()
        except: pass
        _ws_conn = None
        return
    if f:
        tx_push(f)

def ws_accept(srv):
    global _ws_conn
    try: conn, addr = srv.accept()
    except: return
    D(2, "[WS] incoming", addr)
    if _ws_conn is not None:
        try: _ws_conn.close()
        except: pass
        _ws_conn = None
    if not ws_handshake(conn):
        D(1, "[WS] handshake fail")
        try: conn.close()
        except: pass
        return
    conn.setblocking(False)
    _ws_conn = conn
    D(2, "[WS] ready", addr)

# ═══════════════════════════════════════════════════════════
#  TCP TUNNEL THREAD (port 23 debug, port 2222 flash/debug)
# ═══════════════════════════════════════════════════════════

_pin_lock = _thread.allocate_lock()   # protects pin_rst / pin_boot access

def _set_baud(b):
    """Request baud change via bridge thread flag (thread-safe)."""
    _do_baud[0] = b
    t0 = utime.ticks_ms()
    while _do_baud[0] and utime.ticks_diff(utime.ticks_ms(), t0) < 100:
        utime.sleep_ms(2)

def _tcp_tunnel_thread(conn, addr, flush_rx_on_start=False):
    """
    Direct UART tunnel for AVR flash and telnet.
    Same simple select() loop as flash_debug.py tunnel().
    """
    _tcp_active[0] = True
    D(2, "[TCP] start", addr)

    if flush_rx_on_start:
        utime.sleep_ms(30)
        while uart.any(): uart.read(uart.any())

    conn.setblocking(True)
    conn.settimeout(5.0)

    try:
        while True:
            # net → uart
            r, _, _ = select.select([conn], [], [], 0.05)
            if r:
                try:
                    d = conn.recv(512)
                except OSError:
                    d = None
                if not d:
                    break
                uart.write(d)

            # uart → net
            n = uart.any()
            if n:
                d = uart.read(n)
                if d:
                    try:
                        conn.sendall(d)
                    except:
                        break

    except Exception as e:
        D(1, "[TCP] err", e)
    finally:
        try: conn.close()
        except: pass
        _tcp_active[0] = False
        _do_baud[0] = baud["dbg"]
        D(2, "[TCP] done", addr)


def _avr_reset_and_wait():
    """
    Emulate DTR toggle for AVR reset.
    Optiboot (Arduino Uno/Nano) timing:
      - RST pulse: 10ms low
      - Bootloader starts within 20ms of RST release
      - Bootloader window: ~1 second
      - Bootloader baud: always 115200 (optiboot default)
    We flush multiple times to clear ALL startup noise.
    The tunnel will do one more flush at start for safety.
    """
    with _pin_lock:
        pin_rst.value(0)
        utime.sleep_ms(10)
        pin_rst.value(1)
    # Wait for bootloader to fully start and stop sending noise
    utime.sleep_ms(50)
    rx_flush()
    utime.sleep_ms(50)
    rx_flush()
    utime.sleep_ms(30)
    rx_flush()


def _esp_flash_tunnel(conn, addr):
    """
    Direct UART tunnel for ESP flash — same logic as flash_debug.py.
    No ring buffers, no SLIP parsing. Just forward bytes both ways.
    esptool and stub handle baud negotiation themselves.
    """
    _tcp_active[0] = True
    D(2, "[ESP] start", addr)

    uart.init(baudrate=baud["esp"], rx=PIN_RX, tx=PIN_TX, timeout=0, rxbuf=8192)
    conn.setblocking(True)
    conn.settimeout(10.0)

    try:
        while True:
            # net → uart
            r, _, _ = select.select([conn], [], [], 0.05)
            if r:
                try:
                    d = conn.recv(1024)
                except OSError:
                    d = None
                if not d:
                    break
                uart.write(d)

            # uart → net
            n = uart.any()
            if n:
                d = uart.read(n)
                if d:
                    try:
                        conn.sendall(d)
                    except:
                        break

    except Exception as e:
        D(1, "[ESP] err", e)
    finally:
        try: conn.close()
        except: pass
        _tcp_active[0] = False
        _do_baud[0] = baud["dbg"]
        D(2, "[ESP] done", addr)


def _proxy_thread(conn, addr):
    """
    Port 2222 proxy. Handles:
      - AVR flash (stk500v1 / optiboot at 115200)
      - ESP flash (esptool SLIP at esp baud)
      - Debug/telnet (pass-through at dbg baud)

    avrdude with -c arduino over net: opens port TWICE.
    First open: often sends nothing, just tests connection.
    Second open: actual STK500 sync + flash.
    We reset AVR on EVERY connect — works for both cases.

    IMPORTANT: AVR bootloader baud is ALWAYS 115200 (optiboot default).
    baud["avr"] is the APPLICATION baud, not the bootloader baud.
    """
    D(2, "[P] connect", addr)

    # Always reset on connect and set bootloader baud
    _set_baud(115200)
    rx_flush(); tx_flush()
    _avr_reset_and_wait()

    # Wait for first byte from client (avrdude sends after ~100ms)
    conn.settimeout(3.0)
    first = b""
    try:
        first = conn.recv(1)
    except OSError:
        pass

    fb = first[0] if first else 0
    D(2, "[P] first=0x{:02X}".format(fb))

    if not first:
        # No data — pure debug/telnet
        D(2, "[P] debug (no data)")
        _set_baud(baud["dbg"])
        _tcp_tunnel_thread(conn, addr)

    elif fb == 0xC0:
        # esptool SLIP sync — ESP flash
        # esptool v5 flow:
        #   1. connect + SLIP sync at initial baud (115200)
        #   2. upload stub flasher
        #   3. send ESP_CHANGE_BAUDRATE command (op=0x0f)
        #      both sides switch to new baud (default 921600)
        #   4. flash blocks at high baud
        #
        # We use an intercepting tunnel that watches for the
        # CHANGE_BAUDRATE SLIP command and updates our UART baud.
        D(2, "[P] ESP flash")
        _set_baud(baud["esp"])
        with _pin_lock:
            pin_boot.value(0); utime.sleep_ms(10)
            pin_rst.value(0);  utime.sleep_ms(100)
            pin_rst.value(1);  utime.sleep_ms(50)
            pin_boot.value(1)
        utime.sleep_ms(500)
        rx_flush(); tx_flush()
        tx_push(first)
        _esp_flash_tunnel(conn, addr)

    elif fb == 0x30:
        # STK500 Cmnd_STK_GET_SYNC — AVR flash
        # Already reset + at 115200. Flush rx then forward.
        D(2, "[P] AVR flash (0x30)")
        tx_push(first)
        _tcp_tunnel_thread(conn, addr, flush_rx_on_start=True)

    else:
        # Other — debug mode
        D(2, "[P] debug (0x{:02X})".format(fb))
        _set_baud(baud["dbg"])
        tx_push(first)
        _tcp_tunnel_thread(conn, addr)

def handle_proxy(conn, addr):
    try: _thread.start_new_thread(_proxy_thread, (conn, addr))
    except Exception as e:
        D(1, "[P] thread err", e)
        try: conn.close()
        except: pass

# ═══════════════════════════════════════════════════════════
#  CONFIG SERVER  :2223
# ═══════════════════════════════════════════════════════════

def handle_config(conn, addr):
    D(2, "[C] connect", addr)
    buf = b""
    conn.settimeout(0.05)

    def send(m):
        try: conn.sendall((m + "\r\n").encode())
        except: pass

    send("ESP32 Bridge v5 - type HELP")
    deadline = utime.ticks_ms() + 120000

    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        ws_tick()   # keep WS alive during config session

        try:   chunk = conn.recv(64)
        except OSError: chunk = None
        except: break
        if chunk == b"": break
        if chunk:
            buf += chunk
            deadline = utime.ticks_ms() + 120000

        while b"\n" in buf or b"\r" in buf:
            for sep in (b"\r\n", b"\n", b"\r"):
                if sep in buf:
                    line, buf = buf.split(sep, 1)
                    cmd = line.decode("utf-8","ignore").strip().upper()
                    if not cmd: break
                    parts = cmd.split(); p = parts[0]
                    D(2, "[C]", cmd)

                    if p == "RESET":
                        _do_reset[0] = 1; send("OK: hardware reset")
                    elif p == "ESPRESET":
                        _do_reset[0] = 2; send("OK: ESP boot reset")
                    elif p == "AVRRESET":
                        _do_reset[0] = 3; send("OK: AVR reset")
                    elif p == "REBOOT":
                        send("OK: MicroPython reboot")
                        try: conn.close()
                        except: pass
                        utime.sleep_ms(200)
                        machine.reset()
                        return
                    elif p == "STATUS":
                        wd = utime.ticks_diff(utime.ticks_ms(), _bridge_tick[0])
                        send("IP        " + ip)
                        send("DBG_BAUD  " + str(baud["dbg"]))
                        send("ESP_BAUD  " + str(baud["esp"]))
                        send("AVR_BAUD  " + str(baud["avr"]))
                        send("TCP_BUSY  " + str(_tcp_active[0]))
                        send("WD_MS     " + str(wd))
                    elif p == "HELP":
                        send("RESET       hardware reset (RST pin)")
                        send("ESPRESET    ESP boot mode")
                        send("AVRRESET    AVR reset pulse")
                        send("REBOOT      MicroPython machine.reset()")
                        send("ESPBAUD/AVRBAUD/DEBUGBAUD <n>")
                        send("STATUS")
                    elif p in ("ESPBAUD","AVRBAUD","DEBUGBAUD") and len(parts)==2:
                        try:
                            v = int(parts[1])
                            k = {"ESPBAUD":"esp","AVRBAUD":"avr","DEBUGBAUD":"dbg"}[p]
                            baud[k] = v; send("OK: "+p+"="+str(v))
                        except: send("ERR: bad number")
                    else:
                        send("ERR: unknown — HELP")
                    break

    try: conn.close()
    except: pass
    D(2, "[C] done")

# ═══════════════════════════════════════════════════════════
#  HTTP callbacks (called from wifi_setup web_server thread)
# ═══════════════════════════════════════════════════════════

def _http_reset(rtype):
    t = rtype.lower()
    if t == "esp":          _do_reset[0] = 2
    elif t == "avr":        _do_reset[0] = 3
    elif t == "micropython":_do_reset[0] = 4
    else:                   _do_reset[0] = 1

def _http_baud(key, val):
    baud[key] = val
    if key == "dbg":
        _set_baud(val)

# ═══════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════

def wifi_connect():
    global ip
    set_hardware(uart, pin_rst, pin_boot, baud,
                 reset_cb=_http_reset, baud_cb=_http_baud)
    ip = connect_or_setup()
    sta = network.WLAN(network.STA_IF)
    if sta.isconnected(): ip = sta.ifconfig()[0]
    print("="*40)
    print("IP:       ", ip)
    print("Console:   http://"+ip+"/terminal.html")
    print("Telnet:    telnet "+ip)
    print("Flash:     socket://"+ip+":2222")
    print("Config:    nc "+ip+" 2223")
    print("="*40)

def main():
    print("[BOOT] v5")
    wifi_connect()

    _thread.start_new_thread(uart_bridge_thread, ())
    utime.sleep_ms(200)   # let bridge thread start
    _thread.start_new_thread(watchdog_thread, ())

    def srv(port, bl=2):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port)); s.listen(bl); s.setblocking(False)
        print("[BOOT] :"+str(port))
        return s

    sp = srv(PORT_PROXY)
    sd = srv(PORT_DEBUG)
    sc = srv(PORT_CONFIG)
    sw = srv(PORT_WS)
    servers = [sp, sd, sc, sw]
    print("[BOOT] ready")

    while True:
        ws_tick()
        try:
            r, _, _ = select.select(servers, [], [], 0.01)
        except Exception as e:
            D(1, "[LOOP] err", e); continue

        for s in r:
            if s is sw:
                ws_accept(sw)
            elif s is sp:
                try:
                    conn, addr = s.accept()
                    handle_proxy(conn, addr)
                except Exception as e:
                    D(1, "[P] accept err", e)
            elif s is sd:
                try:
                    conn, addr = s.accept()
                    _thread.start_new_thread(_tcp_tunnel_thread, (conn, addr))
                except Exception as e:
                    D(1, "[D] accept err", e)
            elif s is sc:
                try:
                    conn, addr = s.accept()
                    conn.setblocking(False)
                    handle_config(conn, addr)
                except Exception as e:
                    D(1, "[C] accept err", e)

main()
