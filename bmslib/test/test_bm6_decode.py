"""BM6 battery monitor: AES known-answer pairs and realtime decode (#160)."""
import asyncio

import pytest

from bmslib.models.bm6 import Bm6Bt, bm6_encrypt, bm6_decrypt, decode_realtime, aes128_encrypt_block
from bmslib.test._decode_helpers import run_fetch_with_response

# From https://www.tarball.ca/posts/reverse-engineering-the-bm6-ble-battery-monitor/
CMD_CIPHER = bytes.fromhex('697ea0b5d54cf024e794772355554114')
CMD_PLAIN = bytes.fromhex('d1550700000000000000000000000000')
NOTIFY_CIPHER = bytes.fromhex('5a7a41c3a57ca1fa9247f76557c5d618')
NOTIFY_PLAIN = bytes.fromhex('d155070017010004ab00000000020000')


def test_aes_fips197_vector():
    key = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
    pt = bytes.fromhex('00112233445566778899aabbccddeeff')
    assert aes128_encrypt_block(key, pt).hex() == '69c4e0d86a7b0430d8cdb78070b4c55a'


def test_bm6_known_answer_pairs():
    assert bm6_encrypt(CMD_PLAIN) == CMD_CIPHER
    assert bm6_decrypt(NOTIFY_CIPHER) == NOTIFY_PLAIN
    assert bm6_decrypt(bm6_encrypt(NOTIFY_PLAIN)) == NOTIFY_PLAIN


def test_bm6_decode_realtime():
    d = decode_realtime(NOTIFY_PLAIN)
    assert d == dict(temperature=23, state=1, soc=0, voltage=11.95)
    neg = bytearray(NOTIFY_PLAIN)
    neg[3] = 1
    neg[4] = 5
    assert decode_realtime(bytes(neg))['temperature'] == -5


def test_bm6_fetch_sample():
    bms = Bm6Bt("00:11:22:33:44:55", name="bm6")
    sample = run_fetch_with_response(bms, NOTIFY_PLAIN)
    assert sample.voltage == pytest.approx(11.95)
    assert sample.soc == 0
    assert sample.temperatures == [23]
    assert sample.problem is True
    assert sample.battery_mode == 'low_voltage'
    assert sample.battery_charging is False


def test_bm6_notification_routes_by_command_byte():
    async def run():
        bms = Bm6Bt("00:11:22:33:44:55", name="bm6")
        with bms._fetch_futures.acquire(0x07):
            bms._notification_handler(None, NOTIFY_CIPHER)
            return await bms._fetch_futures.wait_for(0x07, 1)

    assert asyncio.run(run()) == NOTIFY_PLAIN


def test_bm6_rejects_wrong_length():
    with pytest.raises(ValueError):
        bm6_decrypt(b'\x00' * 15)
