import asyncio

from bmslib.models.ant import AntBt, AntCommandFuncs, _ant_auth_frame, _ant_command

AUTH = bytes.fromhex('7ea1236a010c 313233343536373839616263 2062aa55')


class _Client:
    def __init__(self):
        self.written = []

    async def write_gatt_char(self, char, data, response=True):
        assert response is False
        self.written.append(bytes(data))
        await asyncio.sleep(0.01)  # a write through an ESPHome proxy yields until it is sent


def _bms():
    bms = AntBt('20:A1:11:02:23:45', name='ant')
    bms.client = _Client()
    bms.char = 'ffe1'
    bms.TIMEOUT = 0.05
    return bms


def test_auth_frame_matches_syssi():
    assert _ant_auth_frame() == AUTH


def test_switch_write_is_preceded_by_auth():
    bms = _bms()
    asyncio.run(bms.set_switch('charge', False))
    assert bms.client.written == [AUTH, bytes.fromhex('7ea1510400 00c8e4aa55')]


def test_status_poll_does_not_split_auth_and_write():
    bms = _bms()
    status = _ant_command(AntCommandFuncs.Status, 0x0000, 0xbe)

    async def poll():
        await asyncio.sleep(0)  # let set_switch send the auth frame first
        try:
            await bms._q(AntCommandFuncs.Status, 0x0000, 0xbe, resp_code=0x11)
        except Exception:
            pass  # no BMS answers here

    async def run():
        await asyncio.gather(bms.set_switch('charge', False), poll())

    asyncio.run(run())
    w = bms.client.written
    assert status in w
    i = w.index(AUTH)
    assert w[i + 1] == _ant_command(AntCommandFuncs.WriteRegister, 0x0004, 0)


# Settings frames, from the ANT app's traffic: ANT-BLE16ZMUB HCI log in syssi/esphome-ant-bms
# (docs/btsnoop_hci_ANT-BLE16ZMUB.log) and the serial captures in its issue #18.
WRITE_CELL_OVP_3650 = bytes.fromhex('7ea1 22 0000 02 420e bdec aa55')
WRITE_DOCP_3000 = bytes.fromhex('7ea1 22 6c00 02 b80b af46 aa55')
ACK_DOCP_3000 = bytes.fromhex('7ea1 42 6c00 02 b80b a6e6 ff0a 0000 1032 aa55')
READ_CELL_UVP = bytes.fromhex('7ea1 02 0c00 02 d9a3 aa55')
REPLY_CELL_UVP_2900 = bytes.fromhex('7ea1 12 0c00 02 540b 667e ff0b 0000 41f2')
READ_CAPACITY = bytes.fromhex('7ea1 02 a200 04 3840 aa55')


def test_setting_frames_match_the_app():
    from bmslib.models.ant import _ant_write_setting_frame, ANT_SETTINGS
    assert _ant_write_setting_frame(0x0000, 3650) == WRITE_CELL_OVP_3650
    assert _ant_write_setting_frame(0x006C, 3000) == WRITE_DOCP_3000
    assert _ant_command(AntCommandFuncs.DeviceInfo, ANT_SETTINGS['cell_uvp'], 2) == READ_CELL_UVP
    assert _ant_command(AntCommandFuncs.DeviceInfo, ANT_SETTINGS['nominal_capacity'], 4) == READ_CAPACITY
    # u32: four value bytes
    f = _ant_write_setting_frame(0x00A2, 245_000_000)
    assert f[5] == 4 and int.from_bytes(f[6:10], 'little') == 245_000_000 and f[-2:] == b'\xaa\x55'


def _answering(bms, reply):
    """Feed reply through the notification handler once the request went out."""
    w = bms.client.write_gatt_char

    async def write(char, data, response=True):
        await w(char, data, response)
        if bytes(data) != AUTH:
            bms._notification_handler(None, reply)

    bms.client.write_gatt_char = write


def test_read_setting():
    bms = _bms()
    _answering(bms, REPLY_CELL_UVP_2900)
    assert asyncio.run(bms.read_setting(0x000C)) == 2900
    assert bms.client.written == [READ_CELL_UVP]


def test_write_setting_is_preceded_by_auth_and_returns_the_echo():
    bms = _bms()
    _answering(bms, ACK_DOCP_3000)
    assert asyncio.run(bms.write_setting(0x006C, 3000)) == 3000
    assert bms.client.written == [AUTH, WRITE_DOCP_3000]


def test_a_reply_for_another_setting_is_refused():
    import pytest
    bms = _bms()
    _answering(bms, REPLY_CELL_UVP_2900)
    with pytest.raises(ValueError):
        asyncio.run(bms.read_setting(0x009E))
