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
