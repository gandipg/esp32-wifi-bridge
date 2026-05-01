# ESP32 WiFi Serial Bridge

A MicroPython firmware that turns an ESP32 into a **WiFi-to-UART bridge** — similar to [esp-link](https://github.com/jeelabs/esp-link) but written entirely in MicroPython.

Flash AVR/Arduino boards over WiFi, monitor serial output in a browser terminal, and tunnel raw UART over TCP — all wirelessly.

---

## Features

- **Web terminal** — xterm.js console at `http://<ip>/terminal.html`, live UART output, keyboard input, macros
- **AVR flash over WiFi** — `avrdude` connects to `:2222`, ESP32 resets the AVR and proxies STK500
- **ESP flash over WiFi** — `esptool` connects to `:2222`, ESP32 puts target into boot mode
- **Telnet/raw TCP** — plain UART tunnel on port `:23`
- **Config server** — telnet to `:2223` for runtime commands (reset, baud, reboot, status)
- **WiFi setup AP** — if no saved credentials, ESP32 broadcasts `ESP32-Setup` hotspot with a config page
- **Watchdog** — auto-reboot if the UART bridge thread stops responding (10 s timeout)
- **Dual UART mode** — uses `IRQ_RXIDLE` if available, falls back to polling

---

## Hardware

Tested on **ESP32 DevKit** (ESP-WROOM-32). Any ESP32 with enough GPIO works.

### Pin wiring

| ESP32 GPIO | Function | Connect to |
|-----------|----------|------------|
| 14 | UART RX | Target TX |
| 27 | UART TX | Target RX |
| 26 | RST | Target RESET (via 100 Ω) |
| 25 | BOOT | Target BOOT/GPIO0 (ESP targets only) |
| GND | GND | Target GND |

> **Note:** GPIO 26/25 are open-drain outputs (pulled high by default). Use a 100 Ω series resistor on RST to avoid conflicts with other reset sources.

### Supported targets

| Target | Flash port | Reset method |
|--------|-----------|--------------|
| Arduino Uno/Nano (optiboot) | `:2222` | RST pulse on TCP connect |
| Arduino Mega | `:2222` | RST pulse on TCP connect |
| ESP8266 / ESP32 | `:2222` | RST + BOOT pins (esptool SLIP detect) |
| Any UART device | `:23` | — |

---

## File structure

```
main.py          — Main application: bridge thread, WebSocket, TCP servers
wifi_setup.py    — WiFi connect/AP setup, HTTP web server (:80)
terminal.html    — Web serial console (xterm.js)
status.html      — Status page
config.html      — WiFi config page
```

---

## Installation

### Requirements

- ESP32 board
- MicroPython firmware ≥ 1.20 (with `_thread`, `select`, `hashlib`)
- [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) or [Thonny](https://thonny.org/) for uploading

### Flash MicroPython (if not already)

```bash
pip install esptool
esptool.py --port /dev/ttyUSB0 erase_flash
esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash -z 0x1000 esp32-20231005-v1.21.0.bin
```

Download firmware from: https://micropython.org/download/ESP32_GENERIC/

### Upload project files

```bash
pip install mpremote

mpremote connect /dev/ttyUSB0 cp main.py :
mpremote connect /dev/ttyUSB0 cp wifi_setup.py :
mpremote connect /dev/ttyUSB0 cp terminal.html :
mpremote connect /dev/ttyUSB0 cp status.html :
mpremote connect /dev/ttyUSB0 cp config.html :
mpremote connect /dev/ttyUSB0 reset
```

Or with Thonny: open each file and use **File → Save as → MicroPython device**.

---

## First boot — WiFi setup

1. Power the ESP32
2. On your phone or laptop, connect to WiFi network **`ESP32-Setup`** (password: `12345678`)
3. Open `http://192.168.4.1` in a browser
4. Enter your WiFi credentials and click Connect
5. ESP32 saves credentials to `config.json` and connects
6. The IP address is printed on the serial console:

```
========================================
IP:        192.168.1.100
Console:   http://192.168.1.100/terminal.html
Telnet:    telnet 192.168.1.100
Flash:     socket://192.168.1.100:2222
Config:    nc 192.168.1.100 2223
========================================
```

---

## Web interface

| URL | Description |
|-----|-------------|
| `http://<ip>/` | Redirects to status page |
| `http://<ip>/status.html` | WiFi status, baud rates |
| `http://<ip>/config.html` | Change WiFi network |
| `http://<ip>/terminal.html` | Live serial console |

### Terminal page

- **Top area** — xterm.js terminal, click to focus, type to send raw bytes
- **Bottom bar** — line input field, press Enter or click SEND to send a line
- **Reset button** — pulses the RST pin (hardware reset of connected device)
- **Baud rate selector** — changes debug UART baud on the fly
- **Macros** — one-click Ctrl+C, Ctrl+D, help(), ls, reboot, etc.
- **Autoscroll** — toggle auto-scroll to bottom
- **Download log** — saves full session to a `.txt` file

---

## Network ports

| Port | Protocol | Description |
|------|----------|-------------|
| 80 | HTTP | Web interface |
| 81 | WebSocket | Serial terminal (used by terminal.html) |
| 23 | TCP | Raw UART tunnel (telnet) |
| 2222 | TCP | Flash proxy (avrdude / esptool) |
| 2223 | TCP | Text config/command interface |

---

## Flashing AVR over WiFi (avrdude)

### platformio.ini

```ini
[env:uno_wifi]
platform = atmelavr
board = uno
framework = arduino
upload_protocol = custom
upload_port = socket://192.168.1.100:2222
upload_flags =
    -C${platformio.packages_dir}/tool-avrdude/avrdude.conf
    -p$BOARD_MCU
    -carduino
    -P$UPLOAD_PORT
    -b115200
```

### Direct avrdude command

```bash
avrdude -v -p atmega328p -c arduino \
  -P socket://192.168.1.100:2222 \
  -b 115200 \
  -D -U flash:w:firmware.hex:i
```

### How it works

When avrdude opens the socket on `:2222`:
1. ESP32 immediately pulses RST pin (emulates DTR toggle)
2. Waits 130 ms for optiboot bootloader to initialize
3. Flushes any UART noise from the AVR startup
4. Waits for first byte from avrdude
5. Detects `0x30` (STK_GET_SYNC) → AVR flash mode
6. Forwards all data bidirectionally at 115200 baud

> **Note:** `ioctl("TIOCMGET"): Inappropriate ioctl for device` warnings from avrdude are normal — they mean avrdude tried to toggle DTR/RTS on the socket (which doesn't exist on TCP). The ESP32 handles reset itself, so these warnings can be ignored.

---

## Flashing ESP8266/ESP32 over WiFi (esptool)

```bash
esptool.py --port socket://192.168.1.100:2222 \
  --baud 460800 \
  write_flash -z 0x0 firmware.bin
```

When esptool opens the socket:
1. ESP32 detects `0xC0` (SLIP sync) as first byte
2. Pulls BOOT pin low + pulses RST (puts target into download mode)
3. Forwards all data at `baud["esp"]` (default 115200, configurable)

---

## Config server (port 2223)

Connect with netcat:

```bash
nc 192.168.1.100 2223
```

### Available commands

| Command | Description |
|---------|-------------|
| `RESET` | Hardware reset — pulses RST pin 100 ms |
| `ESPRESET` | ESP boot mode reset (RST + BOOT pins) |
| `AVRRESET` | AVR reset pulse (10 ms) |
| `REBOOT` | MicroPython `machine.reset()` — reboots the ESP32 bridge itself |
| `STATUS` | Print IP, baud rates, TCP busy flag, watchdog age |
| `DEBUGBAUD <n>` | Set debug UART baud (e.g. `DEBUGBAUD 9600`) |
| `ESPBAUD <n>` | Set ESP flash baud |
| `AVRBAUD <n>` | Set AVR app baud (note: bootloader is always 115200) |
| `HELP` | List commands |

---

## HTTP API

Used by the web pages. Can also be called directly.

| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/api/status` | GET | — | JSON: ip, wifi, ssid, ap, baud |
| `/scan` | GET | — | JSON array of `{ssid, rssi}` |
| `/connect` | POST | `ssid=...&pass=...` | Connect to WiFi network |
| `/reset` | POST | `type=normal\|esp\|avr` | Reset connected device |
| `/baud` | POST | `dbg=115200` | Change debug baud rate |
| `/reboot` | POST | — | Reboot the ESP32 (MicroPython reset) |
| `/startap` | POST | — | Start AP mode |
| `/stopap` | POST | — | Stop AP mode |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                     ESP32                           │
│                                                     │
│  Thread: web_server (:80)                           │
│    HTTP GET  → serve .html files                    │
│    HTTP POST → /reset /baud /connect etc.           │
│                        │                            │
│  Main loop                                          │
│    select() on :81 :2222 :23 :2223                  │
│    ws_tick() — rx_buf → WebSocket → browser         │
│              — browser → tx_buf                     │
│                        │                            │
│  Thread: uart_bridge                                │
│    tx_buf → uart.write()  (chunked, non-blocking)   │
│    uart.read() → rx_buf                             │
│    executes reset/baud requests from flags          │
│                        │                            │
│  Thread: watchdog                                   │
│    machine.reset() if bridge stops for 10s          │
│                                                     │
│  Thread (per conn): tcp_tunnel (:2222 / :23)        │
│    net → tx_buf                                     │
│    rx_buf → net                                     │
└──────────────┬──────────────────────────────────────┘
               │ UART (GPIO 14/27)
         ┌─────▼──────┐
         │   Target   │
         │ Arduino /  │
         │  ESP8266   │
         └────────────┘
```

### Key design decisions

**UART bridge thread owns UART exclusively.** No other thread calls `uart.read()` or `uart.write()` directly. All communication goes through `rx_buf` / `tx_buf` ring buffers. Baud rate changes are requested via a flag (`_do_baud`) and executed by the bridge thread — never from HTTP or TCP threads.

**Lock-free ring buffers.** Single-producer / single-consumer pattern: only the bridge thread writes `rx_buf` head and only the consumer reads `rx_buf` tail. No mutex needed for normal operation.

**Reset via flag.** HTTP thread and config server set `_do_reset[0]` flag. Bridge thread executes the actual pin toggle. This means `/reset` HTTP response returns immediately without blocking.

**AVR flash timing.** RST pulse happens on TCP connect, not on first byte. Optiboot bootloader has a ~1 second window. ESP32 pulses RST, waits 130 ms, triple-flushes UART noise, then waits for avrdude's first STK500 byte.

---

## Configuration

Edit the top of `main.py`:

```python
DEBUG_LEVEL = 2   # 0=off  1=errors  2=info  3=verbose

PIN_RX   = 14     # UART RX from target TX
PIN_TX   = 27     # UART TX to target RX
PIN_RST  = 26     # RST pin to target RESET
PIN_BOOT = 25     # BOOT pin (ESP targets only)

baud = {
    "esp": 115200,   # esptool baud
    "avr": 115200,   # AVR application baud (bootloader always 115200)
    "dbg": 115200,   # debug/telnet baud
}
```

Edit the top of `wifi_setup.py`:

```python
AP_SSID = "ESP32-Setup"   # AP mode SSID
AP_PASS = "12345678"      # AP mode password (min 8 chars)
```

---

## Troubleshooting

### Web terminal shows nothing / stops updating
- Check that port 81 is not blocked by a firewall
- Refresh the page — WebSocket reconnects automatically in 3 s
- Check ESP32 serial console for `[WS]` log lines

### AVR flash fails with "initialization failed"
- Make sure `avrdude -c arduino` is used (not `stk500v2`)
- Baud must be `-b 115200` (optiboot fixed rate)
- Check RST wiring — GPIO 26 must connect to AVR RESET
- Try increasing `utime.sleep_ms` values in `_avr_reset_and_wait()` if bootloader is slow

### avrdude "ioctl TIOCMGET" warnings
- These are **normal** and harmless — avrdude tries DTR/RTS on TCP socket which doesn't support it. Ignore them.

### ESP32 reboots randomly
- Watchdog triggered — bridge thread was blocked for >10 s
- Usually caused by a blocking UART write when target TX is held low
- Check target board power and wiring

### WiFi credentials lost after reboot
- `config.json` is stored in MicroPython filesystem
- If flash was erased, run first-boot WiFi setup again

---

## License

MIT
