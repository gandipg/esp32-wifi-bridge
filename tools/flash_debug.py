"""
flash_debug.py — WiFi/UART proxy (no WiFi code, standalone)
=============================================
  import flash_debug

Ports:
  2222  — flash + debug (auto-detect by first byte)
  23    — debug / telnet (direct tunnel, no reset)
  2223  — config commands over TCP

Auto-detect logic (port 2222):
  0xC0  -> ESP ROM bootloader  (esptool)
  0x30  -> AVR STK500          (avrdude)
  other -> debug tunnel

Config commands (port 2223, connect with: nc <IP> 2223):
  RESET           - normal reset
  ESPRESET        - ESP bootloader reset (GPIO0 + RST)
  AVRRESET        - AVR optiboot reset (RST pulse)
  ESPBAUD <n>     - set ESP flash baud rate
  AVRBAUD <n>     - set AVR flash baud rate
  DEBUGBAUD <n>   - set debug baud rate
  STATUS          - show current config
  HELP            - list commands
"""

import socket, machine, utime, select, _thread
import network

# ── Pin assignments ────────────────────────────────────────────────────
PIN_RX   = 14
PIN_TX   = 27
PIN_RST  = 26
PIN_BOOT = 25

# ── TCP ports ──────────────────────────────────────────────────────────
PORT_PROXY  = 2222
PORT_DEBUG  = 23
PORT_CONFIG = 2223

# ── Baud rate config (can be changed at runtime via port 2223) ─────────
baud = {"esp": 115200, "avr": 115200, "dbg": 115200}

# ── UART init ──────────────────────────────────────────────────────────
uart = machine.UART(1, baudrate=115200,
                    rx=PIN_RX, tx=PIN_TX,
                    timeout=5, rxbuf=4096)

pin_rst  = machine.Pin(PIN_RST,  machine.Pin.OUT, value=1)
pin_boot = machine.Pin(PIN_BOOT, machine.Pin.OUT, value=1)

# ── UART helpers ───────────────────────────────────────────────────────

def set_baud(b):
    uart.init(baudrate=b, rx=PIN_RX, tx=PIN_TX, timeout=5, rxbuf=4096)
    print("[P] baud:", b)

def uart_flush():
    """Drain any leftover bytes from the UART RX buffer."""
    utime.sleep_ms(30)
    while uart.any():
        uart.read(uart.any())

# ── Reset sequences ────────────────────────────────────────────────────

def reset_esp():
    """Pull GPIO0 low then toggle RST — puts ESP into ROM bootloader."""
    pin_boot.value(0)
    utime.sleep_ms(10)
    pin_rst.value(0)
    utime.sleep_ms(100)
    pin_rst.value(1)
    utime.sleep_ms(50)
    pin_boot.value(1)
    utime.sleep_ms(300)
    print("[P] ESP bootloader reset")

def reset_avr():
    """Short RST pulse — triggers AVR optiboot."""
    pin_rst.value(0)
    utime.sleep_ms(10)
    pin_rst.value(1)
    utime.sleep_ms(150)
    print("[P] AVR optiboot reset")

def reset_normal():
    """Standard RST pulse — normal reboot."""
    pin_rst.value(0)
    utime.sleep_ms(100)
    pin_rst.value(1)
    utime.sleep_ms(100)
    print("[P] normal reset")

# ── Bidirectional TCP <-> UART tunnel ──────────────────────────────────

def tunnel(conn, pre=b""):
    """Forward data between TCP socket and UART in both directions.
    Optional `pre` bytes are written to UART first (already-read first byte)."""
    if pre:
        uart.write(pre)
    while True:
        r, _, _ = select.select([conn], [], [], 0.05)
        if r:
            try:
                d = conn.recv(512)
            except:
                break
            if not d:
                break
            uart.write(d)
        if uart.any():
            d = uart.read(uart.any())
            if d:
                try:
                    conn.send(d)
                except:
                    break

# ── Proxy handler — port 2222 ──────────────────────────────────────────

def handle_proxy(conn, addr):
    print("[P] connect:", addr)

    # Always reset AVR first — avrdude cannot trigger reset over TCP itself
    reset_avr()

    # Wait up to 2 s for the first byte to identify the protocol
    r, _, _ = select.select([conn], [], [], 2.0)
    first = b""
    if r:
        try:
            first = conn.recv(1)
        except:
            pass

    if not first:
        # No data received — treat as plain debug session
        print("[P] debug tunnel (no data)")
        set_baud(baud["dbg"])
        tunnel(conn)

    elif first[0] == 0xC0:
        # ESP ROM bootloader sync byte
        print("[P] ESP flash mode")
        set_baud(baud["esp"])
        reset_esp()
        uart_flush()
        tunnel(conn, first)

    elif first[0] == 0x30:
        # AVR STK500 '0' sync byte
        print("[P] AVR flash mode")
        set_baud(baud["avr"])
        uart_flush()
        tunnel(conn, first)

    else:
        # Unknown first byte — treat as debug tunnel
        print("[P] debug tunnel (0x{:02X})".format(first[0]))
        set_baud(baud["dbg"])
        tunnel(conn, first)

    conn.close()
    print("[P] disconnect:", addr)

# ── Debug handler — port 23 (telnet) ──────────────────────────────────

def handle_debug(conn, addr):
    """Direct UART tunnel, no reset, for serial monitoring."""
    print("[D] connect:", addr)
    set_baud(baud["dbg"])
    tunnel(conn)
    conn.close()
    print("[D] disconnect:", addr)

# ── Config server — port 2223 ──────────────────────────────────────────

def config_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT_CONFIG))
    srv.listen(1)
    print("[C] config server ready on port", PORT_CONFIG)

    while True:
        try:
            conn, addr = srv.accept()
        except Exception as e:
            print("[C] accept error:", e)
            utime.sleep(1)
            continue

        print("[C] connect:", addr)

        def send(msg):
            try:
                conn.send((msg + "\r\n").encode())
            except:
                pass

        send("== ESP32 Proxy Config ==")
        send("Type HELP for commands")

        buf = b""
        conn.settimeout(60)
        try:
            while True:
                try:
                    data = conn.recv(64)
                except:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf or b"\r" in buf:
                    for sep in (b"\r\n", b"\n", b"\r"):
                        if sep in buf:
                            line, buf = buf.split(sep, 1)
                            cmd = line.decode("utf-8", "ignore").strip().upper()
                            if not cmd:
                                break
                            parts = cmd.split()
                            p = parts[0]

                            if p == "RESET":
                                reset_normal()
                                send("OK: normal reset")

                            elif p == "ESPRESET":
                                reset_esp()
                                send("OK: ESP bootloader reset")

                            elif p == "AVRRESET":
                                reset_avr()
                                send("OK: AVR optiboot reset")

                            elif p == "STATUS":
                                wlan = network.WLAN(network.STA_IF)
                                send("IP        " + wlan.ifconfig()[0])
                                send("ESPBAUD   " + str(baud["esp"]))
                                send("AVRBAUD   " + str(baud["avr"]))
                                send("DEBUGBAUD " + str(baud["dbg"]))

                            elif p == "HELP":
                                send("RESET           - normal reset")
                                send("ESPRESET        - ESP bootloader reset")
                                send("AVRRESET        - AVR optiboot reset")
                                send("ESPBAUD <n>     - set ESP flash baud rate")
                                send("AVRBAUD <n>     - set AVR flash baud rate")
                                send("DEBUGBAUD <n>   - set debug baud rate")
                                send("STATUS          - show current config")

                            elif p in ("ESPBAUD", "AVRBAUD", "DEBUGBAUD") and len(parts) == 2:
                                try:
                                    v = int(parts[1])
                                    key = {"ESPBAUD":"esp","AVRBAUD":"avr","DEBUGBAUD":"dbg"}[p]
                                    baud[key] = v
                                    send("OK: " + p + " = " + str(v))
                                except:
                                    send("ERR: invalid number")

                            else:
                                send("ERR: unknown command. Type HELP")
                            break
        except Exception as e:
            print("[C] error:", e)
        finally:
            conn.close()
            print("[C] disconnect:", addr)

# ── Debug server — port 23 ─────────────────────────────────────────────

def debug_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT_DEBUG))
    srv.listen(1)
    print("[D] debug server ready on port", PORT_DEBUG)

    while True:
        try:
            conn, addr = srv.accept()
            handle_debug(conn, addr)
        except Exception as e:
            print("[D] error:", e)
            utime.sleep(1)

# ── Startup banner with ready-to-use commands ─────────────────────────

def print_banner(ip):
    p = str(PORT_PROXY)
    c = str(PORT_CONFIG)
    print("")
    print("=" * 60)
    print("  flash_debug proxy  —  " + ip)
    print("=" * 60)
    print("")
    print("  [ESP flash — esptool]")
    print("    esptool.py --chip esp32 \\")
    print("      --port socket://" + ip + ":" + p + " \\")
    print("      --baud 115200 write_flash 0x0 firmware.bin")
    print("")
    print("    # erase flash first:")
    print("    esptool.py --port socket://" + ip + ":" + p + " erase_flash")
    print("")
    print("  [AVR flash — avrdude (optiboot / arduino)]")
    print("    avrdude -c arduino -p atmega328p \\")
    print("      -P net:" + ip + ":" + p + " -b 115200 \\")
    print("      -U flash:w:firmware.hex:i")
    print("")
    print("    # verify only:")
    print("    avrdude -c arduino -p atmega328p \\")
    print("      -P net:" + ip + ":" + p + " -b 115200 \\")
    print("      -U flash:v:firmware.hex:i")
    print("")
    print("  [UART debug tunnel]")
    print("    telnet " + ip)
    print("    # or:")
    print("    nc " + ip + " " + p)
    print("")
    print("  [Config / control]")
    print("    nc " + ip + " " + c)
    print("    # then type: HELP, STATUS, ESPRESET, AVRRESET ...")
    print("")
    print("=" * 60)
    print("")

# ── Main entry point ───────────────────────────────────────────────────

def start():
    wlan = network.WLAN(network.STA_IF)
    ip = wlan.ifconfig()[0] if wlan.isconnected() else "?.?.?.?"

    print_banner(ip)

    # Start config and debug servers in background threads
    _thread.start_new_thread(config_server, ())
    _thread.start_new_thread(debug_server,  ())

    # Main proxy server runs in the foreground (blocks)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT_PROXY))
    srv.listen(1)
    srv.setblocking(False)
    print("[P] proxy ready on port", PORT_PROXY)

    while True:
        r, _, _ = select.select([srv], [], [], 1.0)
        if r:
            try:
                conn, addr = srv.accept()
                conn.setblocking(True)
                handle_proxy(conn, addr)
            except Exception as e:
                print("[P] error:", e)

# Auto-start on import
start()
