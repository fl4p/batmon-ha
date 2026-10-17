"""Felicity Solar RS485 (Voltronic "LIB") frames, copied verbatim out of the live
three-pack bus capture of
https://github.com/KrzysztofHajdamowicz/felicity-battery-modbus (MIT),
`captures/2026-10-10-rs485-bus.log`. The capture timestamp of each frame is in its
name, so every one can be found again in that file.

Three FLB48314TG1 packs at DIP addresses 1, 2 and 3, 9600 8N1. All frames are as
they came off the wire, CRC included -- including the two that lost a byte to the
reporter's USB-RS485 adapter.
"""


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(' ', '').replace('\n', ''))


# --- requests the reporter's master sent (checked against our build_read) -------

REQ_STATUS_1 = h('01 03 00 25 00 11 94 0d')   # addr 1, registers 37..53
REQ_CELLS_1 = h('01 03 00 10 00 15 85 c0')    # addr 1, registers 16..36
REQ_LIMITS_2 = h('02 03 00 70 00 05 84 21')   # addr 2, registers 112..116

# --- status block, registers 37..53 ---------------------------------------------

# 12:47:46.711098 - pack 1, charging at 0.3 A, 53.44 V, SOC 95 %, 4 probes at 28.05 C
STATUS_1 = h('01 03 00 11 00 04 0b c4 0b c4 0b c4 0b c4 00 00 00 00 00 00 00 00'
             '00 00 00 00 00 03 00 00 d0 c0 00 5f 00 05 57 30 7a b9')

# 12:47:48.020317 - pack 2, discharging at 0.8 A, 53.49 V, SOC 96 %, probes 27.05/27.05/27.05/28.05
STATUS_2 = h('02 03 00 11 00 04 0b ba 0b ba 0b ba 0b c4 00 00 00 00 00 00 00 00'
             '00 00 00 00 00 00 00 08 d0 f2 00 60 00 05 57 30 c4 91')

# 13:03:34.812534 - pack 1. This is the frame `docs/protocol.md` works through in
# section 5: "53.41 V, -1.9 A, -101 W, SOC 95 %, 350.0 Ah, 28.1 C" (their sign
# convention: negative = discharging).
STATUS_1_DOC_EXAMPLE = h('01 03 00 11 00 04 0b c4 0b c4 0b c4 0b ba 00 00 00 00 00 00 00 00'
                         '00 00 00 00 00 00 00 13 d0 a2 00 5f 00 05 57 30 12 fc')

# --- cell block, registers 16..36 -----------------------------------------------

# 12:47:54.564043 - pack 1, 16 cells at 3342/3343 mV, cells 17..20 absent (0)
CELLS_1 = h('01 03 00 15 00 10 0d 0e 0d 0e 0d 0f 0d 0e 0d 0e 0d 0e 0d 0f 0d 0e'
            '0d 0e 0d 0e 0d 0f 0d 0f 0d 0f 0d 0e 0d 0e 0d 0e 00 00 00 00 00 00'
            '00 00 ba 58')

# 12:47:55.891989 - pack 2, spread 3340..3344 mV
CELLS_2 = h('02 03 00 15 00 10 0d 10 0d 0f 0d 0e 0d 0f 0d 0f 0d 0f 0d 0e 0d 0e'
            '0d 0c 0d 0e 0d 0e 0d 0f 0d 10 0d 0f 0d 0f 0d 0f 00 00 00 00 00 00'
            '00 00 e9 d6')

# --- limits + status bits, registers 112..116 ------------------------------------

# 13:03:42.700222 (pack 1) and 12:47:59.811827 (pack 2); the packs differ only in
# their charge current limit (48.0 A vs 32.0 A), as docs/protocol.md notes.
LIMITS_1 = h('01 03 00 05 02 40 01 e0 01 e0 06 40 00 c0 8e cf')
LIMITS_2 = h('02 03 00 05 02 40 01 e0 01 40 06 40 00 c0 0d d5')

# --- the adapter's two truncation patterns ---------------------------------------

# 12:48:29.922560 - pack 1 status, CRC high byte lost (the common case, 7 of 10)
TRUNCATED_CRC_1 = h('01 03 00 11 00 04 0b c4 0b c4 0b c4 0b c4 00 00 00 00 00 00 00 00'
                    '00 00 00 00 00 05 00 00 d0 c0 00 5f 00 05 57 30 64')

# 12:48:08.931446 - pack 3 status, slave address lost (3 of 10): the frame starts
# with the function code
TRUNCATED_ADDR_3 = h('03 00 11 00 04 0b c4 0b c4 0b c4 0b c4 00 00 00 00 00 00 00 00'
                     '00 00 00 00 00 00 00 04 d0 de 00 5f 00 05 57 30 98 ef')
