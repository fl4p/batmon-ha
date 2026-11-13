"""Bonding a BMS that only talks to a paired central (`pin:` in the config).

Some firmware serves GATT only to a bonded central: a Felicity pack renamed
`F07…` -> `SolarB_…` by a vendor firmware update refuses reads with
`Insufficient authentication`, or drops the link while BlueZ is still resolving
services (#415, upstream patman15/BMS_BLE-HA#735). The bond has to exist before
any driver connects, and the aiobmsble drivers never pair at all.

Why this is not `BleakClient.pair()`:

* Stock bleak 2.0.0 — the version requirements.txt pins — ships no pairing agent
  (`bleak/backends/bluezdbus/agent.py` does not exist), so it can never answer
  BlueZ's request for a PIN.
* Both that bleak and the fork in venv_bleak_pairing open their D-Bus connection
  in `connect()`, and `pair()` starts with `assert self._bus`. A pack that drops
  the link during service discovery never completes a connect, so the pairing
  call is never reached.

So this module talks to BlueZ directly, and owns its agent: `Device1.Pair()`
brings the link up and runs SMP itself, which is what `bluetoothctl pair` does.
BlueZ then serves GATT to the bonded adapter for every later connection.

**One bus for both.** The agent and the `Pair()` call MUST go over the same D-Bus
connection: BlueZ's `pair_device()` resolves the agent by the *sender* of the
Pair message (`agent = agent_get(sender)`, bluez src/device.c), so an agent
registered on a second connection is simply not found, and BlueZ silently falls
back to whatever default agent the system has — which will not know our PIN.
That is also why this does not use the fork's PairingAgentBlueZDBus: it opens a
bus of its own inside register().
"""

import asyncio
import re

from bmslib.util import get_logger

logger = get_logger()

#: upper bound on a whole bond attempt, including the D-Bus round trips around
#: it. BlueZ's own pairing timeout is 60 s; this is deliberately shorter,
#: because a caller may hold the process-wide ConnectLock while it runs.
BOND_TIMEOUT = 40.0

#: bound on the agent teardown, which runs after the overall timeout has
#: already fired, see the finally block in _bond()
UNREGISTER_TIMEOUT = 5.0

#: how long to scan when BlueZ does not know the device yet
SCAN_TIMEOUT = 8.0

#: bound on stopping that scan, which like the agent teardown may run after the
#: overall timeout has already fired
SCAN_STOP_TIMEOUT = 5.0

#: KeyboardOnly makes BlueZ ask us for the PIN (RequestPinCode / RequestPasskey)
#: instead of picking a Just-Works or numeric-comparison flow -- the same choice
#: as `bluetoothctl agent KeyboardOnly`, which is the documented PIN recipe.
IO_CAPABILITY = 'KeyboardOnly'

BLUEZ = 'org.bluez'
DEVICE_IFACE = 'org.bluez.Device1'
AGENT_IFACE = 'org.bluez.Agent1'
AGENT_MANAGER_IFACE = 'org.bluez.AgentManager1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'

# outcomes of bond_with_pin()
PAIRED = 'paired'
ALREADY_PAIRED = 'already-paired'
UNSUPPORTED = 'unsupported'  # no BlueZ/D-Bus in this stack at all; will not change
FAILED = 'failed'            # could have worked: retry is meaningful

_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')

#: (ServiceInterface class, built agent class) -- keyed by the binding it was
#: built with, so a process that somehow sees both bindings cannot export a
#: dbus_next interface on a dbus_fast bus
_agent_class = (None, None)


def _dbus():
    """dbus_next (shipped by the pairing fork) or dbus_fast (bleak's own), whichever is installed."""
    try:
        from dbus_next import BusType, DBusError, Message, MessageType, Variant
        from dbus_next.aio import MessageBus
        from dbus_next.service import ServiceInterface, method
    except ImportError:
        from dbus_fast import BusType, DBusError, Message, MessageType, Variant
        from dbus_fast.aio import MessageBus
        from dbus_fast.service import ServiceInterface, method
    return dict(MessageBus=MessageBus, BusType=BusType, MessageType=MessageType,
                Message=Message, Variant=Variant, DBusError=DBusError,
                ServiceInterface=ServiceInterface, method=method)


def _make_agent_class(ServiceInterface, method, DBusError):
    """Build the org.bluez.Agent1 implementation.

    Built at call time, not import time: the D-Bus bindings are an optional
    dependency (no BlueZ on the bumble/bluek stacks, none at all on macOS), and
    `method()` has to decorate with the binding that is actually installed. The
    annotations are D-Bus signatures, not Python types -- 'o' object path,
    's' string, 'u' uint32, 'q' uint16.
    """
    global _agent_class
    if _agent_class[0] is ServiceInterface and _agent_class[1] is not None:
        return _agent_class[1]

    class _Agent(ServiceInterface):
        def __init__(self, pin, label):
            super().__init__(AGENT_IFACE)
            self._pin = pin
            self._label = label
            #: set when BlueZ actually asked us for the secret, so a caller can
            #: tell a real passkey bond from a Just-Works one
            self.asked = False

        def _pin_or_reject(self):
            """The configured pin, or a D-Bus rejection BlueZ understands.

            A pin that cannot be delivered must come back as
            org.bluez.Error.Rejected, not as a Python exception during
            marshalling: BlueZ documents RequestPinCode as 1-16 characters and
            RequestPasskey as a number in 0-999999."""
            self.asked = True
            pin = '' if self._pin is None else str(self._pin)
            if not 1 <= len(pin) <= 16:
                raise DBusError(BLUEZ + '.Error.Rejected',
                                'pin must be 1 to 16 characters')
            logger.info('%s: BlueZ asked for the pairing pin', self._label)
            return pin

        @method(name='Release')
        def release(self):
            logger.debug('%s: pairing agent released', self._label)

        @method(name='RequestPinCode')
        def request_pin_code(self, device: 'o') -> 's':  # noqa: F821
            return self._pin_or_reject()

        @method(name='RequestPasskey')
        def request_passkey(self, device: 'o') -> 'u':  # noqa: F821
            pin = self._pin_or_reject()
            try:
                passkey = int(pin)
            except ValueError:
                # BlueZ wants a number here; an alphanumeric pin can only be
                # delivered through RequestPinCode (legacy pairing)
                raise DBusError(BLUEZ + '.Error.Rejected', 'pin is not numeric')
            if not 0 <= passkey <= 999999:
                # outside the documented range: reject, rather than let it fail
                # later in uint32 marshalling
                raise DBusError(BLUEZ + '.Error.Rejected',
                                'passkey must be in 0..999999')
            return passkey

        @method(name='DisplayPinCode')
        def display_pin_code(self, device: 'o', pincode: 's'):  # noqa: F821
            logger.info('%s: device is displaying pin %s', self._label, pincode)

        @method(name='DisplayPasskey')
        def display_passkey(self, device: 'o', passkey: 'u', entered: 'q'):  # noqa: F821
            logger.info('%s: device is displaying passkey %06d', self._label, passkey)

        @method(name='RequestConfirmation')
        def request_confirmation(self, device: 'o', passkey: 'u'):  # noqa: F821
            # numeric comparison: both sides show the same passkey. We have no
            # display, and the pin we hold says the user means to pair.
            logger.info('%s: confirming passkey %06d', self._label, passkey)

        @method(name='RequestAuthorization')
        def request_authorization(self, device: 'o'):  # noqa: F821
            logger.debug('%s: authorizing pairing', self._label)

        @method(name='AuthorizeService')
        def authorize_service(self, device: 'o', uuid: 's'):  # noqa: F821
            logger.debug('%s: authorizing service %s', self._label, uuid)

        @method(name='Cancel')
        def cancel(self):
            logger.info('%s: BlueZ cancelled the pairing request', self._label)

    _agent_class = (ServiceInterface, _Agent)
    return _Agent


def device_path(address: str, adapter=None) -> str:
    """BlueZ object path for a device, the same one bluetoothctl shows."""
    return '/org/bluez/%s/dev_%s' % (adapter or 'hci0', address.upper().replace(':', '_'))


async def _discover_path(address: str, adapter=None):
    """Ask a scan for the device's object path.

    BlueZ only exposes a device object it has seen advertise, so a cold BlueZ
    (add-on just started, BMS not scanned yet) answers UnknownObject for the
    constructed path. Matches on the name as well as the address, because
    `address:` in the config may be a device name (README).
    """
    import bleak
    scanner_kw = {'adapter': adapter} if adapter else {}
    scanner = bleak.BleakScanner(**scanner_kw)
    await scanner.start()
    try:
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < SCAN_TIMEOUT:
            for d in scanner.discovered_devices:
                if d.address.upper() == address.upper() or (d.name or '').strip() == address:
                    path = (d.details or {}).get('path') if isinstance(d.details, dict) else None
                    if path:
                        return path
            await asyncio.sleep(.2)
    finally:
        # Bounded, and BaseException: this also runs when bond_with_pin()'s
        # timeout cancels us, and asyncio then waits for this coroutine to
        # finish -- an unbounded scanner.stop() (it takes a lock, StopDiscovery
        # and SetDiscoveryFilter in bleak's BlueZ backend) would push the whole
        # bond past the bound a caller holding the ConnectLock relies on.
        try:
            await asyncio.wait_for(scanner.stop(), timeout=SCAN_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            # bleak clears its stop callback before awaiting it, so abandoning
            # the wait can leave BlueZ discovering with nobody left to stop it.
            # We cannot fix that from here (it is bleak's shared manager bus,
            # not ours), but it must not be silent -- it shows up later as an
            # adapter that is busy for no visible reason.
            logger.warning('scan for %s could not be stopped within %.0fs, discovery may '
                           'still be running', address, SCAN_STOP_TIMEOUT)
        except BaseException:
            pass
    return None


async def _bond(address, pin, adapter, label, dbus) -> str:
    MessageBus, BusType = dbus['MessageBus'], dbus['BusType']
    MessageType, Message, Variant = dbus['MessageType'], dbus['Message'], dbus['Variant']

    # Hold the bus object BEFORE awaiting connect(): dbus_next installs an
    # event-loop reader before it awaits the Hello reply and does not clean that
    # up on cancellation, so a connect cancelled by our timeout would leak the
    # connection if `bus` were only assigned on success.
    bus = MessageBus(bus_type=BusType.SYSTEM)
    agent_path = '/org/bluez/batmon_agent'
    registered = False
    try:
        try:
            await bus.connect()
        except Exception as e:
            # BlueZ/D-Bus exists in this build but is not reachable right now
            # (daemon still starting, a permission problem). That can change, so
            # it is a retryable failure, NOT "this stack cannot pair".
            logger.warning('%s: cannot reach the system D-Bus: %s', label,
                           str(e) or type(e).__name__)
            return FAILED

        async def call(**kw):
            return await bus.call(Message(destination=BLUEZ, **kw))

        async def prop(path, name):
            reply = await call(path=path, interface=PROPS_IFACE, member='Get',
                               signature='ss', body=[DEVICE_IFACE, name])
            if reply.message_type != MessageType.METHOD_RETURN:
                return None, getattr(reply, 'error_name', None) or 'unknown error'
            return reply.body[0].value, None

        async def paired_state(path):
            """Is there a *stored* bond?

            `Paired` is true for a pairing that completed in this session even
            when nothing was persisted; BlueZ's own Pair() tests `state->bonded`
            before answering AlreadyExists (src/device.c). Treating Paired as a
            bond would skip Pair() for a device that has no stored key and then
            latch it as handled -- exactly the state the reporter of #415 saw
            bluetoothctl distinguish (`Paired: yes, Bonded: yes`). BlueZ exposes
            `Bonded` from 5.65; fall back to `Paired` when it is missing."""
            bonded, err = await prop(path, 'Bonded')
            if err is None:
                return bonded, None
            # Only a MISSING PROPERTY may fall back. Any other error (NoReply, a
            # disconnect, an unknown object) says nothing about whether a bond
            # exists, and falling back would turn it into `Paired` -- which on a
            # paired-but-not-bonded device reads as "already bonded" and skips
            # Pair() entirely.
            if 'InvalidArgs' in err or 'UnknownProperty' in err:
                return await prop(path, 'Paired')
            return None, err

        if _MAC_RE.match(address):
            path = device_path(address, adapter)
            paired, err = await paired_state(path)
        else:
            # a name, not a MAC: BlueZ has no path to guess
            path, paired, err = None, None, 'address is not a MAC'

        if err is not None:
            found = await _discover_path(address, adapter)
            if not found:
                logger.warning('%s: not visible to BlueZ, cannot pair (%s)', label, err)
                return FAILED
            path = found
            paired, err = await paired_state(path)
            if err is not None:
                logger.warning('%s: cannot read pairing state: %s', label, err)
                return FAILED

        if paired:
            logger.info('%s: already bonded', label)
            return ALREADY_PAIRED

        agent = _make_agent_class(dbus['ServiceInterface'], dbus['method'],
                                  dbus['DBusError'])(pin, label)
        bus.export(agent_path, agent)
        reply = await call(path='/org/bluez', interface=AGENT_MANAGER_IFACE,
                           member='RegisterAgent', signature='os',
                           body=[agent_path, IO_CAPABILITY])
        if reply.message_type != MessageType.METHOD_RETURN:
            logger.error('%s: cannot register a pairing agent: %s', label,
                         getattr(reply, 'error_name', None) or 'unknown error')
            return FAILED
        registered = True

        logger.info('%s: pairing (%s)', label, address)
        reply = await call(path=path, interface=DEVICE_IFACE, member='Pair')

        if reply.message_type != MessageType.METHOD_RETURN:
            err = getattr(reply, 'error_name', None) or 'unknown error'
            if err.endswith('.AlreadyExists'):
                logger.info('%s: already bonded', label)
                return ALREADY_PAIRED
            logger.error('%s: pairing failed: %s', label, err)
            return FAILED

        if not agent.asked:
            # BlueZ bonded without ever asking us for the pin (Just Works). The
            # bond is real, but the pin played no part -- say so rather than
            # implying the configured pin was accepted.
            logger.info('%s: bonded without a pin request', label)

        # Trusted lets BlueZ accept the device's own reconnects without an agent
        # present, which the sampling process has no reason to have. Best
        # effort: a bond works without it, so a refusal must not turn a
        # successful pair into a failure -- but do say it happened.
        reply = await call(path=path, interface=PROPS_IFACE, member='Set',
                           signature='ssv', body=[DEVICE_IFACE, 'Trusted', Variant('b', True)])
        if reply.message_type != MessageType.METHOD_RETURN:
            logger.warning('%s: paired, but could not mark it trusted: %s', label,
                           getattr(reply, 'error_name', None) or 'unknown error')

        logger.info('%s: paired', label)
        return PAIRED

    finally:
        # This cleanup also runs when bond_with_pin()'s timeout cancels us, and
        # asyncio.wait_for then waits for the cancelled coroutine to finish --
        # so an UnregisterAgent that hangs would push the whole call past the
        # timeout that is supposed to bound it, while a caller holds the
        # ConnectLock. Bound it separately and keep going.
        #
        # BaseException, not Exception: if this task is cancelled again while
        # awaiting here, CancelledError must not skip the disconnect below.
        # The synchronous disconnect (it is sync in both bindings) is the
        # load-bearing part anyway -- dropping the connection is what releases
        # the agent and, per BlueZ's requestor-exit handling, any pairing still
        # in flight. UnregisterAgent is just the tidy path.
        if registered:
            try:
                await asyncio.wait_for(bus.call(Message(
                    destination=BLUEZ, path='/org/bluez', interface=AGENT_MANAGER_IFACE,
                    member='UnregisterAgent', signature='o', body=[agent_path])),
                    timeout=UNREGISTER_TIMEOUT)
            except BaseException:
                pass
        try:
            bus.disconnect()
        except BaseException:
            pass


async def bond_with_pin(address: str, pin, adapter=None, name=None, timeout=BOND_TIMEOUT) -> str:
    """Bond with `address` using `pin`, through BlueZ's own Pair().

    Returns PAIRED, ALREADY_PAIRED, UNSUPPORTED (no BlueZ in this stack — will
    not change in this process) or FAILED (retryable). Never raises: a pack that
    refuses to bond must not stop the other packs from being read, and the
    connection attempt that follows reports what the device actually does.

    The whole operation is bounded, not just the Pair() call: every step is a
    D-Bus round trip that can hang, and a caller may hold the process-wide
    ConnectLock while this runs.
    """
    label = name or address

    try:
        dbus = _dbus()
    except ImportError:
        # no D-Bus bindings at all: a non-BlueZ stack (bumble, bluek) or a
        # non-Linux host. Nothing to pair against, and that will not change.
        logger.warning('%s: `pin:` needs the BlueZ stack, no D-Bus bindings here', label)
        return UNSUPPORTED

    try:
        return await asyncio.wait_for(_bond(address, pin, adapter, label, dbus), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error('%s: pairing timed out after %.0fs', label, timeout)
        return FAILED
    except Exception as e:
        logger.error('%s: pairing failed: %s', label, str(e) or type(e).__name__)
        return FAILED
