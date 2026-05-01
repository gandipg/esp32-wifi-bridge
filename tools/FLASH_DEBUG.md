# flash_debug.py

A standalone WiFi-to-UART proxy for ESP32, written in MicroPython.

Import it manually from the REPL to get instant AVR/ESP flash support
and a raw serial debug tunnel — no installation, no reboot required.

---

## Requirements

- ESP32 with MicroPython ≥ 1.20
- ESP32 already connected to WiFi before importing
- Target board wired to the ESP32 UART + RST pins

---

## How to start

Open the ESP32 REPL via WebREPL or Thonny, then:

```python
import flash_debug
```

The script prints a banner with ready-to-use commands and starts
listening immediately. Press **Ctrl+C** to stop.

---

## Pin assignments

```python
PIN_RX   = 14   # UART RX  ←  target TX
PIN_TX   = 27   # UART TX  →  target RX
PIN_RST  = 26   # RST      →  target RESET  (via 100 Ω resistor)
PIN_BOOT = 25   # BOOT     →  target GPIO0  (ESP targets only)
```

Edit these constants at the top of the file to match your wiring.

---

## Ports

| Port | Description |
|------|-------------|
| 2222 | Flash proxy — auto-detects AVR / ESP / debug by first byte |
| 23   | Raw UART debug tunnel, no reset |
| 2223 | Text config and control interface |

---

## Baud rates

Defaults — all changeable at runtime via port 2223:

```python
baud = {
    "esp": 115200,   # esptool
    "avr": 115200,   # avrdude  (optiboot bootloader is always 115200)
    "dbg": 115200,   # debug tunnel
}
```

---

## Flashing AVR — avrdude

```bash
avrdude -c arduino -p atmega328p \
  -P net:<IP>:2222 -b 115200 \
  -U flash:w:firmware.hex:i
```

Verify only:

```bash
avrdude -c arduino -p atmega328p \
  -P net:<IP>:2222 -b 115200 \
  -U flash:v:firmware.hex:i
```

**platformio.ini:**

```ini
[env:uno_wifi]
platform = atmelavr
board = uno
framework = arduino
upload_protocol = custom
upload_port = socket://<IP>:2222
upload_flags =
    -C${platformio.packages_dir}/tool-avrdude/avrdude.conf
    -p$BOARD_MCU
    -carduino
    -P$UPLOAD_PORT
    -b115200
```

### Connection sequence

1. TCP connect → RST pin pulsed immediately (10 ms low → high)
2. Wait 150 ms for optiboot to initialize
3. Wait up to 2 s for avrdude to send first byte
4. `0x30` detected (STK_GET_SYNC) → UART flushed → tunnel started
5. Data forwarded bidirectionally until avrdude closes the connection

> `ioctl("TIOCMGET"): Inappropriate ioctl for device` warnings in avrdude
> output are **normal**. avrdude tries DTR/RTS signalling on the TCP
> socket which does not support it. The RST pin is toggled by the ESP32
> directly, so these warnings can be safely ignored.

---

## Flashing ESP8266 / ESP32 — esptool

```bash
esptool.py --chip esp32 \
  --port socket://<IP>:2222 \
  --baud 115200 write_flash 0x0 firmware.bin
```

Erase flash:

```bash
esptool.py --port socket://<IP>:2222 erase_flash
```

### Connection sequence

1. TCP connect → RST pin pulsed (same as AVR path)
2. `0xC0` detected (SLIP sync byte from esptool)
3. BOOT pin pulled low + RST toggled → target enters ROM bootloader
4. UART set to `baud["esp"]`, flushed
5. Tunnel started — esptool handles the rest

---

## UART debug tunnel — port 23

Plain bidirectional pass-through. No reset, no baud change.

```bash
telnet <IP>
# or
nc <IP> 23
```

---

## Config interface — port 2223

```bash
nc <IP> 2223
```

### Commands

| Command | Description |
|---------|-------------|
| `RESET` | RST pin low 100 ms → high |
| `ESPRESET` | BOOT low + RST toggle → ESP enters ROM bootloader |
| `AVRRESET` | RST pulse 10 ms → AVR optiboot |
| `ESPBAUD <n>` | Set ESP flash baud rate |
| `AVRBAUD <n>` | Set AVR baud rate |
| `DEBUGBAUD <n>` | Set debug tunnel baud rate |
| `STATUS` | Show IP and current baud rates |
| `HELP` | List all commands |

Example:

```
$ nc 192.168.1.100 2223
== ESP32 Proxy Config ==
Type HELP for commands
STATUS
IP        192.168.1.100
ESPBAUD   115200
AVRBAUD   115200
DEBUGBAUD 115200
DEBUGBAUD 9600
OK: DEBUGBAUD = 9600
AVRRESET
OK: AVR optiboot reset
```

---

## Architecture

```
import flash_debug
        │
        ├─ _thread ── config_server()  :2223
        ├─ _thread ── debug_server()   :23
        └─ main    ── proxy loop       :2222
                           │
                           └─ handle_proxy(conn)
                                   │
                                   ├─ reset_avr()
                                   ├─ detect protocol
                                   └─ tunnel()  ← blocks until disconnect
```

`handle_proxy()` and `tunnel()` run in the main thread and block for the
duration of each flash session. Only one flash session at a time.
The config and debug servers keep running in their threads throughout.

---

## Limitations

- **No watchdog.** If `tunnel()` hangs, restart manually with Ctrl+C.
- **One flash client at a time** on port 2222.
- **`uart.init()` is not protected.** Do not send `DEBUGBAUD` while a
  flash session is active — it will corrupt the UART state.
- **WiFi must be connected before import.** If WiFi drops, restart
  the script after reconnecting.

---

## Troubleshooting

**AVR: `initialization failed, rc=-1`**
GPIO 26 must be connected to the AVR RESET pin. Without it the RST pulse
never reaches the AVR and optiboot never starts.

**AVR: protocol errors after a successful first sync**
The optiboot window is about 1 second. On slow or congested WiFi,
avrdude may miss the window. Try moving the ESP32 closer to the router
or reducing interference.

**ESP: sync failed / no response**
GPIO 25 must be connected to GPIO0 on the target ESP board. Without it
`reset_esp()` cannot pull GPIO0 low and the target does not enter
download mode.

**Config server stops responding**
The config thread may have crashed. Restart with:

```python
import flash_debug
```

or reboot the ESP32:

```python
import machine; machine.reset()
```
