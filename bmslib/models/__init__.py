from enum import IntEnum, auto




def get_bms_model_class(name):
    import bmslib.models.ant
    import bmslib.models.daly
    import bmslib.models.dummy
    import bmslib.models.jbd
    import bmslib.models.jikong
    import bmslib.models.sok
    import bmslib.models.supervolt
    import bmslib.models.victron

    import bmslib.group

    bms_registry = dict(
        daly=bmslib.models.daly.DalyBt,
        jbd=bmslib.models.jbd.JbdBt,
        jk=bmslib.models.jikong.JKBt,
        ant=bmslib.models.ant.AntBt,
        victron=bmslib.models.victron.SmartShuntBt,
        group_parallel=bmslib.group.VirtualGroupBms,
        # group_serial=bmslib.group.VirtualGroupBms, # TODO
        supervolt=bmslib.models.supervolt.SuperVoltBt,
        sok=bmslib.models.sok.SokBt,
        dummy=bmslib.models.dummy.DummyBt,
    )

    return bms_registry.get(name)


def construct_bms(dev, verbose_log, bt_discovered_devices):
    addr: str = dev['address']

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

    addr = name2addr(addr)

    name: str = dev.get('alias') or dev_by_addr(addr).name

    return bms_class(addr,
                     name=name,
                     verbose_log=verbose_log or dev.get('debug'),
                     psk=dev.get('pin'),
                     adapter=dev.get('adapter'),
                     )
