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
        jk_uart='models.jikong_uart.JKUart',  # RS485/UART TLV protocol; use with address=serial
        daly_uart='models.daly_uart.DalyUart',  # RS485/USB-UART; same A5/04 frames as BLE
        ant='models.ant.AntBt',
        victron='models.victron.SmartShuntBt',
        group_parallel='bmslib.group.VirtualGroupBms',
        # group_serial=bmslib.group.VirtualGroupBms, # TODO
        supervolt='models.supervolt.SuperVoltBt',
        sok='models.sok.SokBt',
        litime='models.litime.LitimeBt',
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


# Stacks a *device* may select. `esphome` is deliberately excluded — it needs
# its own venv (bleak 3 vs the bleak 2 pin), so it is global-only.
PER_DEVICE_BLE_STACKS = frozenset(('bleak', 'bluek', 'bumble'))


def check_ble_stack_config(user_config: dict) -> list:
    """Validate per-device ``ble_stack`` overrides (Approach A, see
    docs/per-device-ble-stack.md) and return the list of overriding devices.

    This is the single fail-fast gate for stack *configuration* errors, run
    before any device is constructed. It enforces two rules and raises
    ``ValueError`` (never a silent pass) on either:

    1. Every per-device ``ble_stack``, when set, must be one of
       ``PER_DEVICE_BLE_STACKS``. A typo or an ``esphome`` at device level is
       rejected here rather than crashing later, deep in ``_resolve_stack``, with
       an uncaught traceback that would take down every configured BMS.
    2. An override imports its stack's bleak-compatible classes directly, which
       only works while the process runs the stock bleak stack. If the global
       ``ble_stack`` swapped ``import bleak`` for a shadow (bumble/bluek) or runs
       the esphome venv, stock bleak is gone process-wide and no override can be
       honoured.

    (Environment failures — a selected package not installed, or an aiobmsble
    model that cannot take an override — are not config errors and surface at
    construction; ``main.py`` converts those to a clean exit too.)
    """
    global_stack = (user_config.get('ble_stack') or 'bleak')
    overriders = []
    for dev in user_config.get('devices', []):
        stack = dev.get('ble_stack')
        if not stack:
            continue
        if stack not in PER_DEVICE_BLE_STACKS:
            raise ValueError(
                "device %s: unsupported per-device ble_stack %r (choose one of "
                "%s; 'esphome' is global-only). See docs/per-device-ble-stack.md."
                % (dev.get('address'), stack, ', '.join(sorted(PER_DEVICE_BLE_STACKS))))
        if stack != global_stack:
            overriders.append(dev)
    if overriders and global_stack != 'bleak':
        raise ValueError(
            "Per-device ble_stack override is only supported when the global "
            "ble_stack is 'bleak' (got '%s'). Offending device(s): %s. "
            "See docs/per-device-ble-stack.md." % (
                global_stack, ', '.join(str(d.get('address')) for d in overriders)))
    return overriders


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

    # Optional `:<spec>` suffix on the type. Currently only the `snoop` type
    # consumes it (comma-separated list of BMS families to actively probe,
    # e.g. `type: snoop:jbd,jk,daly`).
    extra_kwargs = {}
    if ':' in slug:
        slug, probe_spec = slug.split(':', 1)
        slug = slug.strip()
        probe_spec = probe_spec.strip()
        if probe_spec:
            extra_kwargs['probe'] = probe_spec

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

    bms: bmslib.bt.BtBms = bms_class(
        address=addr,
        name=name,
        verbose_log=verbose_log or dev.get('debug'),
        psk=dev.get('pin'),
        adapter=dev.get('adapter'),
        keep_alive=dev.get('keep_alive'),
        ble_stack=dev.get('ble_stack'),
        **extra_kwargs,
    )

    return bms
