"""Tests for bmslib/pairing.py — bonding a BMS through BlueZ's own Device1.Pair().

Neither bleak in the image can do this job: stock bleak 2.0.0 (the
requirements.txt pin) ships no pairing agent at all, and both it and the fork in
venv_bleak_pairing open their D-Bus connection in connect(), so `pair()` begins
with `assert self._bus` — unreachable for a pack that drops the link during
service discovery, which is the #415 symptom.

The load-bearing invariant here is the one that is invisible in a log: BlueZ
resolves the pairing agent by the *sender* of the Pair message
(`agent = agent_get(sender)`, bluez src/device.c), so registering the agent on a
different connection than the one Pair() goes out on means BlueZ silently uses
some other default agent and our PIN is never offered. These tests therefore
check the bus identity, the D-Bus signatures and the bodies — not just that some
call was made — plus the outcomes that must NOT read as success.
"""

import asyncio

import pytest

import bmslib.pairing as pairing

ADDR = "A4:05:FD:13:98:6E"
PATH = "/org/bluez/hci0/dev_A4_05_FD_13_98_6E"
AGENT_PATH = "/org/bluez/batmon_agent"


class _Variant:
    def __init__(self, sig, value):
        self.signature = sig
        self.value = value


class _BusType:
    SYSTEM = "system"


class _MessageType:
    METHOD_RETURN = "method_return"
    ERROR = "error"


class _DBusError(Exception):
    def __init__(self, name, msg):
        super().__init__(msg)
        self.name = name


class _Message:
    def __init__(self, destination=None, path=None, interface=None, member=None,
                 signature=None, body=None):
        self.destination = destination
        self.path = path
        self.interface = interface
        self.member = member
        self.signature = signature
        self.body = body or []


class _Reply:
    def __init__(self, message_type, body=None, error_name=None):
        self.message_type = message_type
        self.body = body or []
        self.error_name = error_name


class _ServiceInterface:
    def __init__(self, name):
        self.interface_name = name


def _method(name=None):
    def deco(fn):
        fn.dbus_method_name = name
        return fn

    return deco


class _FakeBus:
    """A bus that checks what BlueZ would check: signatures, bodies, sender."""

    def __init__(self, paired=False, pair_reply=None, pair_hangs=False, unknown=False,
                 register_reply=None, trusted_reply=None, unregister_hangs=False,
                 bonded=None, no_bonded_prop=False, bonded_error=None):
        # BlueZ >= 5.65 exposes both; `bonded` defaults to `paired` so the
        # existing cases keep meaning what they say
        self.props = {'Paired': paired, 'Bonded': paired if bonded is None else bonded}
        self.no_bonded_prop = no_bonded_prop
        self.bonded_error = bonded_error
        self.paired = paired
        self.pair_reply = pair_reply or _Reply(_MessageType.METHOD_RETURN)
        self.register_reply = register_reply or _Reply(_MessageType.METHOD_RETURN)
        self.trusted_reply = trusted_reply or _Reply(_MessageType.METHOD_RETURN)
        self.pair_hangs = pair_hangs
        self.unregister_hangs = unregister_hangs
        self.unknown = unknown
        self.calls = []
        self.exported = {}
        self.disconnected = False

    async def connect(self):
        return self

    def export(self, path, obj):
        self.exported[path] = obj

    def unexport(self, path):
        self.exported.pop(path, None)

    async def call(self, msg):
        # a real D-Bus call suspends; without a suspension point here a
        # cancelled cleanup would never deliver its CancelledError and the
        # timeout test would pass on code that leaks the bus
        await asyncio.sleep(0)
        assert msg.destination == "org.bluez"
        self.calls.append(msg)
        if msg.member == "Get":
            assert msg.signature == "ss"
            assert msg.body[0] == "org.bluez.Device1"
            name = msg.body[1]
            assert name in ("Bonded", "Paired")
            if self.unknown:
                return _Reply(_MessageType.ERROR,
                              error_name="org.freedesktop.DBus.Error.UnknownObject")
            if name == "Bonded" and self.bonded_error:
                return _Reply(_MessageType.ERROR, error_name=self.bonded_error)
            if name == "Bonded" and self.no_bonded_prop:
                # BlueZ older than 5.65 has no such property
                return _Reply(_MessageType.ERROR,
                              error_name="org.freedesktop.DBus.Error.InvalidArgs")
            return _Reply(_MessageType.METHOD_RETURN, body=[_Variant("b", self.props[name])])
        if msg.member == "RegisterAgent":
            assert msg.signature == "os"
            assert msg.body[0] in self.exported, "agent registered but never exported"
            assert msg.body[1] == pairing.IO_CAPABILITY
            return self.register_reply
        if msg.member == "Pair":
            assert msg.interface == "org.bluez.Device1"
            # BlueZ resolves the agent by the sender of THIS message: it must be
            # the connection the agent was registered on
            assert AGENT_PATH in self.exported, "Pair() sent on a bus with no agent on it"
            if self.pair_hangs:
                await asyncio.sleep(30)
            return self.pair_reply
        if msg.member == "Set":
            assert msg.signature == "ssv"
            assert msg.body[0:2] == ["org.bluez.Device1", "Trusted"]
            assert msg.body[2].value is True
            return self.trusted_reply
        if msg.member == "UnregisterAgent":
            assert msg.signature == "o"
            if self.unregister_hangs:
                await asyncio.sleep(30)
            return _Reply(_MessageType.METHOD_RETURN)
        raise AssertionError("unexpected D-Bus call %s" % msg.member)

    def disconnect(self):
        self.disconnected = True

    def members(self):
        return [m.member for m in self.calls]


@pytest.fixture
def bus_factory(monkeypatch):
    """Install fake D-Bus bindings; hand back the live bus."""

    def install(bus):
        # the real bindings' MessageBus(...) constructs, and connect() returns
        # self -- the caller keeps using the object it constructed
        monkeypatch.setattr(pairing, "_dbus", lambda: dict(
            MessageBus=lambda bus_type=None: bus, BusType=_BusType, MessageType=_MessageType,
            Message=_Message, Variant=_Variant, DBusError=_DBusError,
            ServiceInterface=_ServiceInterface, method=_method))
        # the agent class is built once per process from whichever bindings were
        # found; rebuild it against the fakes
        monkeypatch.setattr(pairing, "_agent_class", (None, None))
        return bus

    return install


def test_device_path_matches_bluez_naming():
    assert pairing.device_path(ADDR) == PATH
    assert pairing.device_path(ADDR.lower(), "hci1") == PATH.replace("hci0", "hci1")


def test_already_paired_short_circuits(bus_factory):
    bus = bus_factory(_FakeBus(paired=True))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.ALREADY_PAIRED
    # no agent and no Pair() for a device that is already bonded
    assert bus.members() == ["Get"]
    assert not bus.exported
    assert bus.disconnected


def test_pairs_over_the_same_bus_the_agent_is_on(bus_factory):
    bus = bus_factory(_FakeBus(paired=False))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.PAIRED
    # the _FakeBus asserts the agent is exported on this very bus when Pair()
    # arrives; BlueZ matches agent to sender, so a second bus would not be found
    assert bus.members() == ["Get", "RegisterAgent", "Pair", "Set", "UnregisterAgent"]
    assert bus.disconnected
    assert bus.calls[2].path == PATH


def test_the_agent_answers_with_the_configured_pin(bus_factory):
    bus = bus_factory(_FakeBus(paired=False))
    asyncio.run(pairing.bond_with_pin(ADDR, "123456"))
    agent = bus.exported[AGENT_PATH]

    assert agent.request_pin_code(PATH) == "123456"     # legacy pin entry
    assert agent.request_passkey(PATH) == 123456        # BlueZ wants a number here
    assert agent.asked is True

    # these must not raise: a device that displays or confirms is still bonding
    agent.display_pin_code(PATH, "123456")
    agent.display_passkey(PATH, 123456, 0)
    agent.request_confirmation(PATH, 123456)


def test_an_alphanumeric_pin_is_rejected_not_crashed(bus_factory):
    bus = bus_factory(_FakeBus(paired=False))
    asyncio.run(pairing.bond_with_pin(ADDR, "abc123"))
    agent = bus.exported[AGENT_PATH]
    assert agent.request_pin_code(PATH) == "abc123"
    with pytest.raises(_DBusError):
        agent.request_passkey(PATH)  # not a number: reject, do not ValueError


def test_failed_agent_registration_does_not_pair(bus_factory):
    """Pairing without our agent would hand BlueZ no PIN and let some other
    default agent answer — never do it."""
    bus = bus_factory(_FakeBus(paired=False, register_reply=_Reply(
        _MessageType.ERROR, error_name="org.freedesktop.DBus.Error.AccessDenied")))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.FAILED
    assert "Pair" not in bus.members()
    assert bus.disconnected


def test_already_exists_error_is_a_bond(bus_factory):
    bus_factory(_FakeBus(paired=False, pair_reply=_Reply(
        _MessageType.ERROR, error_name="org.bluez.Error.AlreadyExists")))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.ALREADY_PAIRED


def test_pairing_error_is_reported_as_failure(bus_factory):
    bus = bus_factory(_FakeBus(paired=False, pair_reply=_Reply(
        _MessageType.ERROR, error_name="org.bluez.Error.AuthenticationFailed")))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.FAILED
    # the agent is released and the bus dropped even on the error path
    assert "UnregisterAgent" in bus.members()
    assert bus.disconnected


def test_a_failed_trusted_still_counts_as_paired(bus_factory):
    bus = bus_factory(_FakeBus(paired=False, trusted_reply=_Reply(
        _MessageType.ERROR, error_name="org.freedesktop.DBus.Error.AccessDenied")))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.PAIRED
    assert bus.disconnected


def test_a_hanging_pair_times_out_and_cleans_up(bus_factory):
    bus = bus_factory(_FakeBus(paired=False, pair_hangs=True))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456", timeout=0.05)) == pairing.FAILED
    assert bus.disconnected, "a timed-out bond must still drop the bus"


def test_a_hanging_teardown_cannot_outlast_the_bond(monkeypatch):
    """The agent teardown runs after the overall timeout has already fired, and
    asyncio waits for it — so if it hangs it silently extends the bond past the
    bound a caller holding the ConnectLock is relying on. Calibrated against the
    known-bad version: with the UNREGISTER_TIMEOUT wait_for removed, this test
    fails on its own outer 3 s guard."""
    monkeypatch.setattr(pairing, "UNREGISTER_TIMEOUT", 0.05)
    bus = _FakeBus(paired=False, pair_hangs=True, unregister_hangs=True)

    monkeypatch.setattr(pairing, "_dbus", lambda: dict(
        MessageBus=lambda bus_type=None: bus, BusType=_BusType, MessageType=_MessageType,
        Message=_Message, Variant=_Variant, DBusError=_DBusError,
        ServiceInterface=_ServiceInterface, method=_method))
    monkeypatch.setattr(pairing, "_agent_class", (None, None))

    async def run():
        return await asyncio.wait_for(
            pairing.bond_with_pin(ADDR, "123456", timeout=0.05), timeout=3)

    assert asyncio.run(run()) == pairing.FAILED
    assert bus.disconnected


def test_unknown_device_scans_and_fails_if_not_found(bus_factory, monkeypatch):
    """BlueZ only knows a device it has seen advertise. If a scan doesn't find
    it either, that is a failure — never a quiet OK."""
    bus_factory(_FakeBus(unknown=True))

    async def no_scan_hit(address, adapter=None):
        return None

    monkeypatch.setattr(pairing, "_discover_path", no_scan_hit)
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.FAILED


def test_a_name_instead_of_a_mac_is_resolved_by_scanning(bus_factory, monkeypatch):
    """`address:` may be a device name (README). No object path can be guessed
    from one, so it must come from the scanner — not be built from the name."""
    bus = bus_factory(_FakeBus(paired=False))

    async def scan_hit(address, adapter=None):
        assert address == "SolarB_E33B_26110698"
        return PATH

    monkeypatch.setattr(pairing, "_discover_path", scan_hit)
    assert asyncio.run(pairing.bond_with_pin("SolarB_E33B_26110698", "123456")) == pairing.PAIRED
    # the first Get was never sent against a path built out of the name
    assert all("SolarB" not in (m.path or "") for m in bus.calls)


def test_unreachable_dbus_is_retryable_not_unsupported(monkeypatch):
    """A daemon that is not up yet can come up; that must not be cached as
    'this stack cannot pair'."""

    class _Failing:
        def __init__(self, bus_type=None):
            pass

        async def connect(self):
            raise OSError("connection refused")

    monkeypatch.setattr(pairing, "_dbus", lambda: dict(
        MessageBus=_Failing, BusType=_BusType, MessageType=_MessageType,
        Message=_Message, Variant=_Variant, DBusError=_DBusError,
        ServiceInterface=_ServiceInterface, method=_method))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.FAILED


def test_missing_dbus_bindings_is_unsupported(monkeypatch):
    """On a non-BlueZ stack (bumble, bluek) or a non-Linux host there are no
    bindings at all — that will not change, and must not raise into the caller."""

    def no_dbus():
        raise ImportError("no dbus_fast")

    monkeypatch.setattr(pairing, "_dbus", no_dbus)
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.UNSUPPORTED


def test_a_stored_bond_is_bonded_not_merely_paired(bus_factory):
    """BlueZ reports `Paired` for a pairing that completed in this session even
    when no key was stored; its own Pair() tests `bonded`. Taking Paired for a
    bond would skip Pair() and then latch the device as handled while it still
    has no stored key — the exact distinction the reporter of #415 saw in
    `bluetoothctl info`."""
    bus = bus_factory(_FakeBus(paired=True, bonded=False))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.PAIRED
    assert "Pair" in bus.members(), "paired-but-not-bonded must still be bonded"
    assert [m.body[1] for m in bus.calls if m.member == "Get"] == ["Bonded"]


def test_falls_back_to_paired_on_older_bluez(bus_factory):
    """`Bonded` exists from BlueZ 5.65. An older daemon answers InvalidArgs, and
    then `Paired` is the best available answer — not a failure."""
    bus = bus_factory(_FakeBus(paired=True, no_bonded_prop=True))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.ALREADY_PAIRED
    assert [m.body[1] for m in bus.calls if m.member == "Get"] == ["Bonded", "Paired"]


def test_an_unknown_device_is_not_read_as_a_missing_property(bus_factory, monkeypatch):
    """The Bonded probe fails for two very different reasons; only the missing
    property may fall back. An unknown object must go to the scanner."""
    bus_factory(_FakeBus(unknown=True))
    scanned = []

    async def scan(address, adapter=None):
        scanned.append(address)
        return None

    monkeypatch.setattr(pairing, "_discover_path", scan)
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.FAILED
    assert scanned == [ADDR]


@pytest.mark.parametrize("pin, why", [
    ("1000000", "above the documented 0..999999 passkey range"),
    ("-1", "negative: not a uint32"),
])
def test_out_of_range_passkeys_are_rejected(bus_factory, pin, why):
    bus = bus_factory(_FakeBus(paired=False))
    asyncio.run(pairing.bond_with_pin(ADDR, pin))
    agent = bus.exported[AGENT_PATH]
    with pytest.raises(_DBusError):
        agent.request_passkey(PATH)  # %s


@pytest.mark.parametrize("pin", ["", "x" * 17])
def test_unusable_pins_are_rejected_as_dbus_errors(bus_factory, pin):
    """BlueZ documents 1..16 characters. A pin outside that has to come back as
    org.bluez.Error.Rejected, not blow up in marshalling."""
    bus = bus_factory(_FakeBus(paired=False))
    asyncio.run(pairing.bond_with_pin(ADDR, pin))
    agent = bus.exported[AGENT_PATH]
    with pytest.raises(_DBusError):
        agent.request_pin_code(PATH)


def test_a_hanging_scanner_stop_cannot_outlast_the_bond(bus_factory, monkeypatch):
    """The scan's cleanup runs after the bond timeout has already cancelled us.
    asyncio delivers CancelledError only once, so that cleanup then runs to
    completion — an unbounded scanner.stop() would hang the whole bond while a
    caller holds the ConnectLock. Calibrated against the known-bad version:
    without the SCAN_STOP_TIMEOUT wait_for, this fails on its outer guard."""
    monkeypatch.setattr(pairing, "SCAN_STOP_TIMEOUT", 0.05)
    bus = bus_factory(_FakeBus(unknown=True))  # forces the scan fallback
    stopped = []

    class _HangingScanner:
        def __init__(self, **kw):
            self.discovered_devices = []

        async def start(self):
            pass

        async def stop(self):
            stopped.append(True)
            await asyncio.sleep(30)

    import bleak
    monkeypatch.setattr(bleak, "BleakScanner", _HangingScanner)

    async def run():
        # the bond is cancelled while the scan loop is still running; the outer
        # guard is what fails if the cleanup is not bounded
        return await asyncio.wait_for(
            pairing.bond_with_pin(ADDR, "123456", timeout=0.05), timeout=3)

    assert asyncio.run(run()) == pairing.FAILED
    assert stopped, "the scan must be stopped"
    assert bus.disconnected


def test_a_cancelled_bus_connect_still_disconnects(monkeypatch):
    """dbus_next installs an event-loop reader before awaiting Hello and does
    not clean it up on cancellation, so a bus whose connect() is cancelled by
    our timeout must still be disconnected by us."""
    disconnected = []

    class _SlowBus:
        def __init__(self, bus_type=None):
            pass

        async def connect(self):
            await asyncio.sleep(30)
            return self

        def disconnect(self):
            disconnected.append(True)

    monkeypatch.setattr(pairing, "_dbus", lambda: dict(
        MessageBus=_SlowBus, BusType=_BusType, MessageType=_MessageType,
        Message=_Message, Variant=_Variant, DBusError=_DBusError,
        ServiceInterface=_ServiceInterface, method=_method))
    monkeypatch.setattr(pairing, "_agent_class", (None, None))

    async def run():
        return await asyncio.wait_for(
            pairing.bond_with_pin(ADDR, "123456", timeout=0.05), timeout=3)

    assert asyncio.run(run()) == pairing.FAILED
    assert disconnected, "a bus cancelled mid-connect was leaked"


def test_a_failed_bonded_read_is_not_a_bond(bus_factory):
    """Only a MISSING property may fall back to `Paired`. Any other error says
    nothing about whether a bond exists — falling back would turn a NoReply on
    a paired-but-not-bonded device into "already bonded" and skip Pair()."""
    bus = bus_factory(_FakeBus(paired=True, bonded=False,
                               bonded_error="org.freedesktop.DBus.Error.NoReply"))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.FAILED
    assert "Pair" not in bus.members()
    # and it must not have quietly asked for Paired instead
    assert [m.body[1] for m in bus.calls if m.member == "Get"] == ["Bonded"]


@pytest.mark.parametrize("missing", [
    "org.freedesktop.DBus.Error.InvalidArgs",
    "org.freedesktop.DBus.Error.UnknownProperty",
])
def test_a_missing_bonded_property_falls_back(bus_factory, missing):
    bus = bus_factory(_FakeBus(paired=True, bonded=False, bonded_error=missing))
    assert asyncio.run(pairing.bond_with_pin(ADDR, "123456")) == pairing.ALREADY_PAIRED
    assert [m.body[1] for m in bus.calls if m.member == "Get"] == ["Bonded", "Paired"]
