"""
BM6 / BM2 style Bluetooth car battery monitor (lead-acid 12 V "battery health guardian", #160).

The device is a voltage + temperature logger with an SOC estimate, it has no current
sensor and no cells. Its GATT service 0xFFF0 carries one write characteristic (FFF3)
and one notify characteristic (FFF4). Every 16-byte frame in either direction is
AES-128-CBC encrypted with a fixed key and a zero IV, which for a single block is plain
AES-ECB. Protocol sources:

* https://www.tarball.ca/posts/reverse-engineering-the-bm6-ble-battery-monitor/ (key,
  realtime command and reply layout, known-answer pair used in the unit test)
* https://github.com/Rafciq/BM6 (HA integration, state byte, SOC byte)
* https://github.com/JeffWDH/bm6-battery-monitor

Realtime request, plain: d1 55 07 00 .. 00 (16 bytes). Reply, plain (hex nibbles):
  d155 07 | ss | tt | st | pp | vvvv | aaaa | dddd | ....
  ss   temperature sign (01 = below zero)        nibbles 6-7
  tt   temperature magnitude, degC               nibbles 8-9
  st   state 0=ok 1=low voltage 2=charging       nibbles 10-11
  pp   SOC %                                     nibbles 12-13
  vvvv voltage * 100                             nibbles 14-17
  aaaa/dddd rapid acceleration/deceleration counters (ignored)

AES is implemented here in a few lines of pure Python because batmon's venv has no
crypto library and we only ever encrypt/decrypt one 16-byte block per poll.
"""
import asyncio
import math

from bmslib.bms import BmsSample
from bmslib.bt import BtBms

BM6_KEY = bytes([108, 101, 97, 103, 101, 110, 100, 255, 254, 48, 49, 48, 48, 48, 48, 57])

CMD_REALTIME = bytes.fromhex('d1550700000000000000000000000000')
CMD_VERSION = bytes.fromhex('d1550100000000000000000000000000')

_STATES = {0: 'ok', 1: 'low_voltage', 2: 'charging'}


# --- minimal AES-128 (single block) ------------------------------------------------

def _xtime(a):
    a <<= 1
    return (a ^ 0x1b) & 0xff if a & 0x100 else a


def _build_tables():
    sbox = [0] * 256
    p = q = 1
    while True:
        # multiply p by 3
        p = p ^ _xtime(p)
        # divide q by 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xff
        if q & 0x80:
            q ^= 0x09
        x = q ^ (q << 1) ^ (q << 2) ^ (q << 3) ^ (q << 4)
        sbox[p] = (x ^ (x >> 8) ^ 0x63) & 0xff
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


_SBOX, _INV_SBOX = _build_tables()


def _expand_key(key: bytes):
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[t[1]] ^ rcon, _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
            rcon = _xtime(rcon)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _mix_columns(s, inverse=False):
    m = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    out = list(s)
    for c in range(4):
        col = s[c * 4:c * 4 + 4]
        for r in range(4):
            out[c * 4 + r] = (_mul(col[0], m[(0 - r) % 4]) ^ _mul(col[1], m[(1 - r) % 4]) ^
                              _mul(col[2], m[(2 - r) % 4]) ^ _mul(col[3], m[(3 - r) % 4]))
    return out


def _shift_rows(s, inverse=False):
    out = list(s)
    for r in range(1, 4):
        for c in range(4):
            src = ((c - r) if inverse else (c + r)) % 4
            out[c * 4 + r] = s[src * 4 + r]
    return out


def aes128_encrypt_block(key: bytes, block: bytes) -> bytes:
    rk = _expand_key(key)
    s = [b ^ k for b, k in zip(block, rk[0])]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = _shift_rows(s)
        if rnd != 10:
            s = _mix_columns(s)
        s = [b ^ k for b, k in zip(s, rk[rnd])]
    return bytes(s)


def aes128_decrypt_block(key: bytes, block: bytes) -> bytes:
    rk = _expand_key(key)
    s = [b ^ k for b, k in zip(block, rk[10])]
    for rnd in range(9, -1, -1):
        s = _shift_rows(s, inverse=True)
        s = [_INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        if rnd != 0:
            s = _mix_columns(s, inverse=True)
    return bytes(s)


def bm6_encrypt(plain: bytes) -> bytes:
    assert len(plain) == 16
    return aes128_encrypt_block(BM6_KEY, plain)


def bm6_decrypt(cipher: bytes) -> bytes:
    if len(cipher) != 16:
        raise ValueError(f"BM6 frame must be 16 bytes, got {len(cipher)}")
    return aes128_decrypt_block(BM6_KEY, bytes(cipher))


def decode_realtime(plain: bytes) -> dict:
    """Decode a decrypted realtime reply (d1 55 07 ...)."""
    if plain[:3] != b'\xd1\x55\x07':
        raise ValueError(f"not a BM6 realtime frame: {plain.hex()}")
    temp = plain[4]
    if plain[3] == 1:
        temp = -temp
    return dict(
        temperature=temp,
        state=plain[5],
        soc=plain[6],
        voltage=int.from_bytes(plain[7:9], 'big') / 100,
    )


class Bm6Bt(BtBms):
    UUID_RX = '0000fff4-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000fff3-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        kwargs.setdefault('_uses_pin', False)
        super().__init__(address, **kwargs)
        self._last_response = None

    def _notification_handler(self, sender, data):
        try:
            plain = bm6_decrypt(data)
        except Exception as e:
            self.logger.warning("BM6 undecodable notification %s: %s", bytes(data).hex(), e)
            return
        self._last_response = plain
        self.logger.debug("BM6 notify plain %s", plain.hex())
        if plain[:2] == b'\xd1\x55':
            self._fetch_futures.set_result(plain[2], plain)

    async def connect(self, **kwargs):
        try:
            await super().connect(**kwargs)
        except Exception as e:
            self.logger.info("normal connect failed (%s), connecting with scanner", e)
            await self._connect_with_scanner(**kwargs)
        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def _q(self, cmd: int):
        plain = {0x07: CMD_REALTIME, 0x01: CMD_VERSION}[cmd]
        with self._fetch_futures.acquire(cmd):
            await self.client.write_gatt_char(self.UUID_TX, data=bm6_encrypt(plain))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        plain = await self._q(0x07)
        d = decode_realtime(plain)
        state = _STATES.get(d['state'], 'unknown_%d' % d['state'])
        return BmsSample(
            voltage=d['voltage'],
            current=math.nan,  # the BM6 has no current sensor
            soc=d['soc'],
            temperatures=[d['temperature']],
            battery_charging=d['state'] == 2,
            battery_mode=state,
            problem=d['state'] == 1,
            problem_code=d['state'] if d['state'] == 1 else 0,
        )

    async def fetch_voltages(self):
        return []

    async def set_switch(self, switch: str, state: bool):
        self.logger.info("BM6 has no switches")

    def debug_data(self):
        return self._last_response


async def main():
    bms = Bm6Bt('50:54:7B:24:30:10', name='bm6')
    await bms.connect()
    print(await bms.fetch())
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
