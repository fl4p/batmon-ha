import importlib
from functools import partial

import bleak

import bmslib.bt
from bmslib.util import get_logger

logger = get_logger()


def get_bms_model_class(name: str):
    #

    # Type aliases resolved before the registry / aiobmsble lookup below.
    # `sok` now routes to aiobmsble's ABC-BMS driver (`abc` -> abc_bms): current
    # SOK/ABC-firmware batteries answer with CC-prefixed frames that the legacy
    # models/sok.py (EE-prefix, 'w'-terminated) never completes, producing the
    # recurring "timeout waiting for 193" (#390, #222, #178). The old driver is
    # still reachable as `sok_legacy` for early firmware it works on.
    name = {'sok': 'abc'}.get(name, name)

    if False:
        import bmslib.models.ant
        import bmslib.models.daly
        import bmslib.models.daly2
        import bmslib.models.dummy
        import bmslib.models.jbd
        import bmslib.models.jikong
        import bmslib.models.sok
        import bmslib.models.supervolt
        import bmslib.models.victron
        import bmslib.models.litime
        import bmslib.models.bm6

    bms_registry = dict(
        daly='models.daly.DalyBt',
        daly2='models.daly2.Daly2Bt',
        jbd='models.jbd.JbdBt',
        jk='models.jikong.JKBt',  # auto detect
        jk_24s='models.jikong.JKBt_24s',  # https://github.com/syssi/esphome-jk-bms/blob/main/esp32-ble-example.yaml#L6
        jk_32s='models.jikong.JKBt_32s',
        jk_uart='models.jikong_uart.JKUart',  # RS485/UART TLV protocol; use with address=serial
        daly_uart='models.daly_uart.DalyUart',  # RS485/USB-UART; same A5/04 frames as BLE
        pace_uart='models.pace.PaceUart',  # PACE "paceic" RS232/RS485 ASCII protocol (#276)
        ant='models.ant.AntBt',
        victron='models.victron.SmartShuntBt',
        group_parallel='bmslib.group.VirtualGroupBms',
        # group_serial=bmslib.group.VirtualGroupBms, # TODO
        supervolt='models.supervolt.SuperVoltBt',
        sok_legacy='models.sok.SokBt',  # pre-2023 SOK/ABC firmware; `sok` -> aiobmsble abc_bms (see alias above)
        litime='models.litime.LitimeBt',
        bm6='models.bm6.Bm6Bt',  # BM6/BM2-style AES-encrypted 12 V car battery monitor (#160)
        bm2='models.bm6.Bm2Bt',  # Quicklynks BM2 / Ancel BM200, other key, pushes frames unsolicited (#41)
        basen='models.basen.BasenBt',  # Basen BLE (0xFA00/01/02), ported from syssi/esphome-basen-bms
        basen_uart='models.basen_uart.BasenUart',  # Basen RS232/RS485 (paceic-unrelated), ported from GHswitt/esphome-basen
        noname_modbus='models.noname_modbus.NoNameModbusBt',  # generic Modbus-RTU-over-NUS
        dummy='models.dummy.DummyBt',
        snoop='models.snoop.SnoopBt',  # GATT dumper for reverse-engineering new BMS
    )

    mod_class = bms_registry.get(name)
    if bms_registry.get(name):
        if mod_class.startswith('models'):
            mod_class = 'bmslib.' + mod_class
        ss = mod_class.split('.')
        mod = importlib.import_module('.'.join(ss[:-1]))  # __import__ is discouraged
        return getattr(mod, ss[-1])

    try:
        if 0:
            from aiobmsble.basebms import BaseBMS
            from typing import Type
        if name.endswith('_ble'):
            # map any `_ble` devices to `aiobmsble`
            # DEPRECATED
            name = name[:-4]
        if name.endswith('_aiobmsble'):  # map all `_aiobmsble` to `aiobmsble`
            name = name[:-len('_aiobmsble')]
        type_ = name + '_bms'
        try:
            mod = importlib.import_module(f'aiobmsble.bms.{type_}')
        except ImportError as e:
            try:
                mod = importlib.import_module(f'bmslib.bms_ble.plugins.{type_}')
            except ImportError:
                raise e

        from bmslib.models import BLE_BMS_wrap
        return partial(BLE_BMS_wrap.BMS, type=type_, blebms_class=mod.BMS)
    except:
        logger.exception('aiobmsble error', exc_info=True)
        return None


def device_address(dev: dict) -> str:
    """Normalized `address:` of a configured device. The HA add-on schema types it
    as free text, so users paste stray whitespace; everyone comparing against
    `'serial'` must go through here or the comparison silently misses (#380)."""
    return str(dev.get('address') or '').strip()


def is_serial_device(dev: dict) -> bool:
    """True for a wired BMS, whose `adapter:` is a tty path, not a BT controller."""
    return device_address(dev) == 'serial'


def construct_bms(dev: dict, verbose_log: bool, bt_discovered_devices: list):
    addr: str = device_address(dev)

    if not addr or addr.startswith('#'):
        return None

    slug = str(dev['type'] or '').strip()

    # Optional `:<spec>` suffix on the type, forwarded to the model as
    # `type_spec`. Consumed by `snoop` (comma-separated BMS families to actively
    # probe, e.g. `type: snoop:jbd,jk,daly`) and by `daly_uart` (the RS485 board
    # number, e.g. `type: daly_uart:2`).
    extra_kwargs = {}
    if ':' in slug:
        slug, type_spec = slug.split(':', 1)
        slug = slug.strip()
        type_spec = type_spec.strip()
        if type_spec:
            extra_kwargs['type_spec'] = type_spec

    bms_class = get_bms_model_class(slug)

    if bms_class is None:
        logger.warning('Unknown device type %s', dev)
        return None

    if dev.get('debug'):
        logger.info('Verbose log for %s enabled', addr)

    def name2addr(name: str):
        return next((d.address for d in bt_discovered_devices if (d.name or "").strip() == name.strip()), name)

    def dev_by_addr(address: str):
        dev = next((d for d in bt_discovered_devices if d.address.lower() == address.strip().lower()), None)
        if not dev:
            raise Exception("Can't resolve device name %s, not discovered" % address)
        return dev

    if addr == "serial" and not dev.get('alias'):
        raise ValueError('with `address=serial` you need to specify `alias`')
    addr = name2addr(addr)

    name: str = dev.get('alias') or dev_by_addr(addr).name

    try:
        bms: bmslib.bt.BtBms = bms_class(
            address=addr,
            name=name,
            verbose_log=verbose_log or dev.get('debug'),
            psk=dev.get('pin'),
            adapter=dev.get('adapter'),
            keep_alive=dev.get('keep_alive'),
            **extra_kwargs,
        )
    except Exception:
        # A per-device config problem must stay per-device. An unknown `type` or
        # a commented-out address already skip with a warning; a constructor that
        # rejects its arguments (e.g. `type: daly_uart:0` -- the board number is
        # 1-based) used to propagate out of main()'s device loop and abort the
        # whole add-on, so every *other* configured battery never started.
        logger.exception('Cannot construct %s device %s, skipping it', slug, name)
        return None

    return bms
