import threading
import time

from bmslib.util import get_logger
from bmslib.wired.transport import SerialTransport, StdioTransport

logger = get_logger()


class SharedSerialPortError(Exception):
    """Two BMS want the same serial device with incompatible link settings."""


class _SharedSerialPort(object):
    """The physical port: transport, reader thread, callback fan-out, refcount.

    One instance per device path, shared by every BMS configured on that path so
    several units can sit on one RS485 bus (#398). Each BMS holds its own
    :class:`SerialBleakClientWrapper` handle onto it; this object owns nothing
    BMS-specific.

    Everything here is touched from two threads -- the reader thread and the
    event loop -- so mutations of the shared state take ``_lock``.
    """

    # Consecutive read errors tolerated before the reader thread gives up, and
    # the pause between retries. Class attributes so tests can shrink them.
    RX_MAX_ERRORS = 20
    RX_ERROR_SLEEP = 0.5

    def __init__(self, port: str, baudrate: int, **serial_kwargs):
        self.t = SerialTransport(port, baudrate=baudrate, **serial_kwargs)
        self.settings = (baudrate, serial_kwargs.get('eol', b'\n'),
                         serial_kwargs.get('timeout', None))
        # callback registry: key -> (owner handle, callback)
        self.callback = {}
        self._lock = threading.Lock()
        self._open_refs = 0
        # Set when the reader thread has stopped for good. Anything waiting on a
        # response must be able to tell "nothing arrived yet" from "nothing will
        # ever arrive again", instead of timing out identically forever.
        self.rx_thread_error = None
        # Serializes request/response on the bus. RS485 is half duplex, so with
        # concurrent_sampling two BMS would otherwise talk over each other.
        # Created lazily: an asyncio.Lock wants the running loop.
        self._bus_lock = None
        # Event loop the decoders run on, so received bytes can be handed over
        # instead of being decoded on the reader thread. See _dispatch().
        self._loop = None
        self._rx_thread = None
        self._stop = threading.Event()
        self._start_reader()

    def bind_loop(self, loop):
        self._loop = loop

    # --- reader thread -----------------------------------------------------

    def _start_reader(self):
        self._rx_thread = threading.Thread(target=self._on_receive, daemon=True,
                                           name='serial-rx-%s' % self.t.port)
        self._rx_thread.start()

    def ensure_reader(self):
        """Respawn the reader if a previous incarnation gave up.

        Without this a transient USB dropout that outlasted RX_MAX_ERRORS killed
        the thread for the life of the process: the port still opened, writes
        still succeeded, and every command timed out forever with no reader to
        deliver the reply. sampling.py disconnects a BMS after repeated errors
        and reconnects on the next cycle, so this makes it self-heal.
        """
        with self._lock:
            if self._stop.is_set():
                return  # shut down on purpose; don't resurrect it
            if self._rx_thread is not None and self._rx_thread.is_alive():
                return
            logger.warning('restarting the serial reader thread for %s (previous '
                           'one died: %s)', self.t.port, self.rx_thread_error)
            self.rx_thread_error = None
            self._start_reader()

    def _on_receive(self):
        # Any exception escaping this loop used to kill the reader thread
        # silently: the port stayed "connected", writes kept succeeding, and
        # every command timed out forever with no hint as to why. Log it and
        # record it so the failure is attributable.
        errors = 0
        while not self._stop.is_set():
            try:
                data = self.t.is_open and self.t.read()
                if data:
                    errors = 0
                    self._dispatch(data)
            except Exception as e:
                errors += 1
                # Bounded retry: a transient read error (USB re-enumeration) is
                # worth riding out, a permanent one must not spin the CPU or
                # pretend the link is alive.
                if errors >= self.RX_MAX_ERRORS:
                    self.rx_thread_error = e
                    logger.error('serial reader thread giving up on %s after %d '
                                 'consecutive errors: %s', self.t.port, errors, e)
                    return
                logger.warning('serial read error on %s (%d): %s', self.t.port, errors, e)
                self._stop.wait(self.RX_ERROR_SLEEP)
            self._stop.wait(0.1) # todo block

    def shutdown(self):
        """Stop the reader and close the port, regardless of refcount.

        For process shutdown and for tests: without it the reader thread keeps
        the device open forever, so anything trying to close the other end of
        the link (or just re-open the port) blocks.
        """
        self._stop.set()
        t, self._rx_thread = self._rx_thread, None
        if t is not None and t.is_alive():
            t.join(timeout=2)
        self._open_refs = 0
        try:
            self.t.close()
        except Exception as e:
            logger.debug('closing %s: %s', self.t.port, e)

    def _deliver(self, data):
        with self._lock:
            targets = list(self.callback.values())
        # Every BMS on the bus sees every byte; each filters for its own
        # replies (Daly puts the board number in byte 1).
        for _owner, callback in targets:
            try:
                callback(self, data)
            except Exception:
                logger.exception('serial rx callback failed on %d bytes', len(data))

    def _dispatch(self, data):
        """Hand received bytes to the decoders on the event loop thread.

        Decoding them here on the reader thread would resolve the asyncio
        Future for the pending request *without waking the loop*
        (FuturesPool.set_result calls Future.set_result directly, which is not
        thread-safe). The loop then stays asleep in its selector until its next
        timer -- which is the request's own 12 s timeout. The reply is correct
        but arrives 12 s late, and on a shared bus every BMS pays that in turn.
        BLE never hit this because bleak already calls back on the loop.

        Doing the handover also keeps each decoder's buffers single-threaded.
        """
        loop = self._loop
        if loop is None:
            return self._deliver(data)   # not connected yet, or a direct unit test
        try:
            loop.call_soon_threadsafe(self._deliver, data)
        except RuntimeError as e:
            # loop already closed (shutdown, or a test that outlived its loop)
            logger.debug('serial rx could not reach the event loop (%s), '
                         'decoding inline', e)
            self._deliver(data)

    # --- refcounted open/close --------------------------------------------

    def acquire(self):
        """Open the port for one more user. Idempotent while others hold it."""
        with self._lock:
            self._open_refs += 1
        if not self.t.is_open:
            self.t.open()
        self.ensure_reader()

    def release(self):
        """Drop one user. The port closes only when the last one lets go --
        otherwise one BMS disconnecting would take its bus siblings down."""
        with self._lock:
            self._open_refs = max(0, self._open_refs - 1)
            if self._open_refs:
                return False
        self.t.close()
        return True

    @property
    def users(self):
        return self._open_refs

    def register(self, key, owner, callback):
        with self._lock:
            prev = self.callback.get(key)
            if prev is not None and prev[0] is not owner:
                raise SharedSerialPortError(
                    'two BMS on %s both claim %r. On a shared RS485 bus each unit '
                    'needs its own address (for Daly: `type: daly_uart:<board>`)'
                    % (self.t.port, key))
            self.callback[key] = (owner, callback)

    def unregister(self, key, owner):
        with self._lock:
            if self.callback.get(key, (None,))[0] is owner:
                self.callback.pop(key, None)

    def bus_lock(self):
        import asyncio
        if self._bus_lock is None:
            self._bus_lock = asyncio.Lock()
        return self._bus_lock


# Live shared ports, keyed by device path.
_shared_ports = {}
_shared_ports_lock = threading.Lock()


def _get_shared_port(port: str, baudrate: int, **serial_kwargs) -> _SharedSerialPort:
    settings = (baudrate, serial_kwargs.get('eol', b'\n'), serial_kwargs.get('timeout', None))
    with _shared_ports_lock:
        sp = _shared_ports.get(port)
        if sp is None:
            sp = _SharedSerialPort(port, baudrate, **serial_kwargs)
            _shared_ports[port] = sp
        elif sp.settings != settings:
            # e.g. jk_uart (115200) and daly_uart (9600) pointed at one adapter.
            # One wire cannot run two baud rates; say so instead of silently
            # opening the device twice and letting the reader threads race.
            raise SharedSerialPortError(
                '%s is already used at baud=%s eol=%r timeout=%s; cannot also use it at '
                'baud=%s eol=%r timeout=%s. Different BMS families need separate adapters'
                % ((port,) + sp.settings + settings))
        return sp


def _reset_shared_ports():
    """Stop and forget every shared port (tests, and process shutdown)."""
    with _shared_ports_lock:
        ports = list(_shared_ports.values())
        _shared_ports.clear()
    for sp in ports:
        sp.shutdown()


class SerialBleakClientWrapper(object):
    """Per-BMS handle onto a (possibly shared) serial port.

    Connected state is tracked per handle, not per port. That matters: BtBms's
    __aenter__ skips connect() when is_connected, so if two BMS shared one
    is_connected flag the second would never call start_notify and would sit
    there receiving nothing.
    """

    def __init__(self, address, baudrate: int = 115200, **kwargs):
        self.address = address
        # Forward optional framing knobs (eol / timeout) a BMS class exposes via
        # its SERIAL_KWARGS; unknown kwargs are ignored so existing callers keep
        # the readline() default.
        serial_kwargs = {k: kwargs[k] for k in ('eol', 'timeout') if k in kwargs}
        self.port = _get_shared_port(address.split(':')[-1], baudrate, **serial_kwargs)
        self.services = []
        self._connected = False
        self._keys = set()

    # --- shared-port passthroughs -----------------------------------------

    @property
    def t(self):
        """The underlying transport (shared). Kept for callers that reach in."""
        return self.port.t

    @property
    def callback(self):
        return self.port.callback

    @property
    def rx_thread_error(self):
        return self.port.rx_thread_error

    @property
    def rx_bytes(self):
        """Bytes read off the port since open. Port-level truth, independent of
        whether any notify callback was registered to consume them."""
        return getattr(self.port.t, 'rx_bytes', None)

    @property
    def shared(self):
        """True when another BMS is using this same port."""
        return self.port.users > 1

    def bus_lock(self):
        return self.port.bus_lock()

    async def get_services(self):
        return self.services

    async def connect(self, timeout=None):
        # Bind the reader thread to whichever loop the decoders run on. Done
        # here (not at construction) because BtBms builds the client before the
        # loop exists.
        import asyncio
        self.port.bind_loop(asyncio.get_running_loop())
        if self._connected:
            return
        self.port.acquire()
        self._connected = True

    async def disconnect(self):
        for key in list(self._keys):
            self.port.unregister(key, self)
        self._keys.clear()
        if not self._connected:
            return
        self._connected = False
        self.port.release()

    @property
    def is_connected(self):
        return self._connected and self.port.t.is_open

    async def start_notify(self, char, callback):
        self.port.register(char, self, callback)
        self._keys.add(char)

    async def stop_notify(self, char):
        self.port.unregister(char, self)
        self._keys.discard(char)

    async def write_gatt_char(self, _char, data):
        self.port.t.write(data)


class SerialServiceStub():
    def __init__(self, uuid):
        self.uuid = uuid

class SerialCharStub():
    def __init__(self, uuid_or_handle, property_name):
        self.uuid_or_handle = uuid_or_handle
        self.property_name = property_name

    # Value-based identity so a stub is a stable dict key. Serial BMS models
    # build a fresh SerialCharStub on every connect() and register it as the
    # notify-callback key. Without this, each reconnect adds a *new* key
    # (stop_notify never removes the old one, because BtBms.stop_notify is gated
    # on `client.services`, which is always empty for serial transports), so the
    # same _notification_handler accumulates and every received chunk is
    # delivered N times, corrupting frame reassembly from the second poll
    # onward. Equal stubs collapse to one key instead.
    #
    # NOTE this makes the uuid the bus-wide identity of a BMS: two units sharing
    # one port MUST build stubs with different uuids (DalyUart appends the board
    # number), or _SharedSerialPort.register rejects the second as a duplicate.
    def __eq__(self, other):
        return (isinstance(other, SerialCharStub)
                and self.uuid_or_handle == other.uuid_or_handle
                and self.property_name == other.property_name)

    def __hash__(self):
        return hash((self.uuid_or_handle, self.property_name))

    def __repr__(self):
        return 'SerialCharStub(%r, %r)' % (self.uuid_or_handle, self.property_name)
