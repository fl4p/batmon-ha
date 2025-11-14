import importlib
from functools import partial

import bleak

import bmslib.bt
from bmslib.util import get_logger

logger = get_logger()


def get_bms_model_class(name):
    #

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

    bms_registry = dict(
        daly='models.daly.DalyBt',
        daly2='models.daly2.Daly2Bt',
        jbd='models.jbd.JbdBt',
        jk='models.jikong.JKBt',  # auto detect
        jk_24s='models.jikong.JKBt_24s',  # https://github.com/syssi/esphome-jk-bms/blob/main/esp32-ble-example.yaml#L6
        jk_32s='models.jikong.JKBt_32s',
        ant='models.ant.AntBt',
        victron='models.victron.SmartShuntBt',
        group_parallel='bmslib.group.VirtualGroupBms',
        # group_serial=bmslib.group.VirtualGroupBms, # TODO
        supervolt='models.supervolt.SuperVoltBt',
        sok='models.sok.SokBt',
        litime='models.litime.LitimeBt',
        dummy='models.dummy.DummyBt',
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
            name = name[:-4]
        type_ = name + '_bms'
        mod = importlib.import_module(f'aiobmsble.bms.{type_}')
        from bmslib.models import BLE_BMS_wrap
        return partial(BLE_BMS_wrap.BMS, type=type_, blebms_class=mod.BMS)
    except:
        logger.exception('aiobmsble error', exc_info=True)
        return None


def construct_bms(dev: dict, verbose_log: bool, bt_discovered_devices: list[bleak.BLEDevice]):
    addr: str = str(dev['address'] or '').strip()

    if not addr or addr.startswith('#'):
        return None

    bms_class = get_bms_model_class(dev['type'])

    if bms_class is None:
        logger.warning('Unknown device type %s', dev)
        return None

    if dev.get('debug'):
        logger.info('Verbose log for %s enabled', addr)

    def name2addr(name: str):
        return next((d.address for d in bt_discovered_devices if (d.name or "").strip() == name.strip()), name)

    def dev_by_addr(address: str):
        dev = next((d for d in bt_discovered_devices if d.address == address), None)
        if not dev:
            raise Exception("Can't resolve device name %s, not discovered" % address)
        return dev

    if addr == "serial" and not dev.get('alias'):
        raise ValueError('with `address=serial` you need to specify `alias`')
    addr = name2addr(addr)

    name: str = dev.get('alias') or dev_by_addr(addr).name

    bms: bmslib.bt.BtBms = bms_class(
        address=addr,
        name=name,
        verbose_log=verbose_log or dev.get('debug'),
        psk=dev.get('pin'),
        adapter=dev.get('adapter'),
        keep_alive=dev.get('keep_alive'),
    )

    return bms
