"""Stdlib-only HTTP + WebSocket server for the batmon GUI.

No third-party dependency, deliberately. The add-on image installs no compiler
(see Dockerfile lines 88-98) and aiohttp publishes no musllinux i686 wheel and no
pure-Python wheel, so on the `i386` and `armhf` arches it would have to build from
source and the GUI would silently not exist. Measured 2026-09-15 against PyPI:

    aiohttp 3.14.3   musl armv7l yes | musl i686 NO | py3-none-any NO

Its dependencies (yarl/frozenlist/propcache) do ship pure-Python fallbacks, so
aiohttp itself is the blocker. ~150 lines of RFC 6455 framing is the cheaper side
of that trade. Everything here is asyncio streams + hashlib/base64/struct.

The module owns no state: it renders whatever `GuiState` hands it.
"""
import asyncio
import base64
import hashlib
import mimetypes
import os
import struct
import time
from typing import Optional

from bmslib.util import get_logger

logger = get_logger()

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# A client that cannot keep up gets its pending frame replaced, never queued
# without bound: state frames are snapshots, so the newest one is the only one
# that matters. See _Client.offer().
MAX_CLIENTS = 8
LAGGED_GRACE_S = 30.0

# Opcodes
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsProtocolError(Exception):
    pass


def _accept_key(client_key: str) -> str:
    return base64.b64encode(
        hashlib.sha1(client_key.encode('ascii') + WS_GUID).digest()
    ).decode('ascii')


def encode_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """Server->client frame. Never masked, per RFC 6455 5.1."""
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < (1 << 16):
        head.append(126)
        head += struct.pack('!H', n)
    else:
        head.append(127)
        head += struct.pack('!Q', n)
    return bytes(head) + payload


async def read_frame(reader: asyncio.StreamReader):
    """Read one client frame. Returns (fin, opcode, payload).

    Enforces the parts of RFC 6455 that actually bite: a client frame MUST be
    masked (5.1), control frames MUST be <=125 bytes and MUST NOT be fragmented
    (5.5), and reserved bits MUST be zero unless an extension negotiated them.
    """
    hdr = await reader.readexactly(2)
    b0, b1 = hdr[0], hdr[1]
    fin = bool(b0 & 0x80)
    if b0 & 0x70:
        raise WsProtocolError("reserved bits set")
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    if not masked:
        raise WsProtocolError("client frame not masked")
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack('!H', await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack('!Q', await reader.readexactly(8))[0]
    if opcode >= 0x8:
        if n > 125:
            raise WsProtocolError("control frame too long")
        if not fin:
            raise WsProtocolError("fragmented control frame")
    mask = await reader.readexactly(4)
    data = bytearray(await reader.readexactly(n))
    for i in range(n):
        data[i] ^= mask[i & 3]
    return fin, opcode, bytes(data)


class _Client:
    """One websocket peer, with a single-slot outbox.

    Dropping an *older* frame is only safe because every push we send is a
    complete snapshot of the nodes it names. A partial frame that named only
    the nodes that changed could not be dropped like this -- losing pack A's
    only update would strand A until it happened to change again. So the
    coalescing happens here, by union-ing the changed node set, and the frame
    is built from current state at send time.
    """

    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self._wake = asyncio.Event()
        self._pending_nodes = set()      # node ids whose state changed
        self._pending_full = False       # send a complete snapshot
        self._pending_system = False
        self.lagged_since: Optional[float] = None
        self.closed = False

    def offer(self, node_ids=None, full=False, system=False):
        if full:
            self._pending_full = True
            self._pending_nodes.clear()
        elif node_ids:
            self._pending_nodes |= set(node_ids)
        if system:
            self._pending_system = True
        self._wake.set()

    def take(self):
        nodes, full, system = self._pending_nodes, self._pending_full, self._pending_system
        self._pending_nodes = set()
        self._pending_full = False
        self._pending_system = False
        self._wake.clear()
        return nodes, full, system

    async def send(self, text: str):
        self.writer.write(encode_frame(text.encode('utf-8')))
        await self.writer.drain()

    async def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.write(encode_frame(struct.pack('!H', code), OP_CLOSE))
            await self.writer.drain()
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass


class GuiServer:
    def __init__(self, state, host='0.0.0.0', port=8099, push_period=1.0,
                 allow_direct=True, web_root=None):
        self.state = state
        self.host = host
        self.port = port
        self.push_period = max(0.2, float(push_period))
        self.allow_direct = allow_direct
        self.web_root = web_root or os.path.join(os.path.dirname(__file__), 'web')
        self._clients = []
        self._server = None
        self._pusher = None
        self._loop = None
        self._last_rev = 0
        self._last_config_hash = None

    @property
    def url(self):
        return 'http://%s:%d/' % (self.host, self.port)

    async def start(self):
        # GuiState is plain dicts with no locks; that is only sound because the
        # sink writes and these reads are on the same loop. Make it an assertion,
        # not a comment.
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._on_conn, self.host, self.port)
        self._pusher = asyncio.create_task(self._push_loop())

    async def stop(self):
        if self._pusher:
            self._pusher.cancel()
        for c in list(self._clients):
            await c.close(1001)
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

    # ---------------- HTTP ----------------

    async def _on_conn(self, reader, writer):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=20)
            if not request_line:
                writer.close()
                return
            parts = request_line.decode('latin-1').split()
            if len(parts) < 2:
                writer.close()
                return
            method, target = parts[0], parts[1]

            headers = {}
            total = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=20)
                if line in (b'\r\n', b'\n', b''):
                    break
                total += len(line)
                if total > 32768 or len(headers) > 100:
                    raise WsProtocolError("header block too large")
                k, _, v = line.decode('latin-1').partition(':')
                headers[k.strip().lower()] = v.strip()

            path = target.split('?', 1)[0]
            base = headers.get('x-ingress-path', '')
            if base and path.startswith(base):
                path = path[len(base):] or '/'

            # Ingress requests are authenticated by Home Assistant. A direct hit on
            # the host port is NOT -- and X-Ingress-Path is a header any LAN client
            # can set, so it authenticates nobody and is never used as a credential.
            # The only real control is refusing direct connections outright.
            if not self.allow_direct and not self._is_local(writer):
                await self._respond(writer, 403, b'direct access disabled', 'text/plain')
                return

            if path == '/ws' or path == '/api/ws':
                await self._ws_handshake(reader, writer, headers)
                return

            await self._route(method, path, base, writer)
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            logger.debug('gui conn error: %s', e)
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    def _is_local(writer):
        try:
            peer = writer.get_extra_info('peername')
            return bool(peer) and peer[0] in ('127.0.0.1', '::1')
        except Exception:
            return False

    async def _route(self, method, path, base, writer):
        if path in ('/api/system',):
            await self._json(writer, self.state.system_doc())
        elif path in ('/api/state',):
            await self._json(writer, self.state.state_doc(partial=False))
        elif path.startswith('/api/node/'):
            nid = path[len('/api/node/'):]
            doc = self.state.state_doc(node_ids=[nid], partial=True)
            await self._json(writer, doc)
        elif path == '/api/health':
            await self._json(writer, {'ok': True, 'nodes': len(self.state.node_ids()),
                                      'clients': len(self._clients), 'ts': time.time()})
        elif path == '/api/command':
            await self._json(writer, {'v': 1, 'type': 'ack', 'ts': time.time(),
                                      'data': {'ok': False,
                                               'error': {'code': 'not_implemented',
                                                         'message': 'writes arrive in a later phase'}}},
                             status=501)
        else:
            await self._static(path, base, writer)

    async def _static(self, path, base, writer):
        rel = 'index.html' if path in ('/', '') else path.lstrip('/')
        full = os.path.normpath(os.path.join(self.web_root, rel))
        if not full.startswith(os.path.realpath(self.web_root)) and not full.startswith(self.web_root):
            await self._respond(writer, 403, b'forbidden', 'text/plain')
            return
        if not os.path.isfile(full):
            # SPA fallback
            full = os.path.join(self.web_root, 'index.html')
            if not os.path.isfile(full):
                await self._respond(writer, 404,
                                    b'<h1>batmon</h1><p>UI bundle not built.</p>'
                                    b'<p>The JSON API is live: '
                                    b'<a href="api/state">api/state</a></p>', 'text/html')
                return
        with open(full, 'rb') as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        headers = {}
        if full.endswith('index.html'):
            # The Supervisor serves the panel under /api/hassio_ingress/<token>/ and
            # forwards that prefix here. The SPA builds every URL from this base.
            body = body.replace(b'%%BATMON_BASE%%', base.encode('latin-1'))
            headers['Cache-Control'] = 'no-store'
        else:
            headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        await self._respond(writer, 200, body, ctype, headers)

    async def _json(self, writer, doc, status=200):
        from bmslib.wire import model
        body = model.encode(doc).encode('utf-8')
        await self._respond(writer, status, body, 'application/json',
                            {'Cache-Control': 'no-store'})

    async def _respond(self, writer, status, body: bytes, ctype: str, headers=None):
        reason = {200: 'OK', 403: 'Forbidden', 404: 'Not Found',
                  501: 'Not Implemented', 503: 'Service Unavailable'}.get(status, 'OK')
        out = ['HTTP/1.1 %d %s' % (status, reason),
               'Content-Type: %s' % ctype,
               'Content-Length: %d' % len(body),
               'Connection: close']
        for k, v in (headers or {}).items():
            out.append('%s: %s' % (k, v))
        writer.write(('\r\n'.join(out) + '\r\n\r\n').encode('latin-1') + body)
        try:
            await writer.drain()
        finally:
            writer.close()

    # ---------------- WebSocket ----------------

    async def _ws_handshake(self, reader, writer, headers):
        key = headers.get('sec-websocket-key')
        if not key or headers.get('upgrade', '').lower() != 'websocket':
            await self._respond(writer, 400 if key else 404, b'bad upgrade', 'text/plain')
            return
        if len(self._clients) >= MAX_CLIENTS:
            await self._respond(writer, 503, b'too many viewers', 'text/plain')
            return
        writer.write(('HTTP/1.1 101 Switching Protocols\r\n'
                      'Upgrade: websocket\r\n'
                      'Connection: Upgrade\r\n'
                      'Sec-WebSocket-Accept: %s\r\n\r\n' % _accept_key(key)).encode('latin-1'))
        await writer.drain()

        client = _Client(writer)
        self._clients.append(client)
        try:
            # system first, always: a client must never interpret state against
            # topology it has not seen.
            await client.send(self._encode(self.state.system_doc()))
            await client.send(self._encode(self.state.state_doc(partial=False)))
            writer_task = asyncio.create_task(self._writer_loop(client))
            await self._reader_loop(client, reader)
            writer_task.cancel()
        except Exception as e:
            logger.debug('ws client ended: %s', e)
        finally:
            if client in self._clients:
                self._clients.remove(client)
            await client.close()

    @staticmethod
    def _encode(doc):
        from bmslib.wire import model
        return model.encode(doc)

    async def _reader_loop(self, client, reader):
        while not client.closed:
            fin, op, data = await read_frame(reader)
            if op == OP_CLOSE:
                break
            if op == OP_PING:
                client.writer.write(encode_frame(data, OP_PONG))
                await client.writer.drain()
            # text/binary/pong from the client are ignored: this is a push feed.

    async def _writer_loop(self, client):
        while not client.closed:
            await client._wake.wait()
            nodes, full, system = client.take()
            try:
                if system:
                    await client.send(self._encode(self.state.system_doc()))
                if full:
                    await client.send(self._encode(self.state.state_doc(partial=False)))
                elif nodes:
                    await client.send(self._encode(
                        self.state.state_doc(node_ids=sorted(nodes), partial=True)))
            except (ConnectionResetError, BrokenPipeError):
                break
            except Exception as e:
                logger.debug('ws send failed: %s', e)
                break

    async def _push_loop(self):
        while True:
            try:
                await asyncio.sleep(self.push_period)
                if not self._clients:
                    self._last_rev = self.state.revision
                    continue
                rev = self.state.revision
                changed = self.state.changed_since(self._last_rev)
                self._last_rev = rev

                system = False
                try:
                    h = self.state.system_doc()['data'].get('config_hash')
                    if h != self._last_config_hash:
                        self._last_config_hash = h
                        system = True
                except Exception:
                    pass

                if not changed and not system:
                    continue
                for c in list(self._clients):
                    c.offer(node_ids=changed, system=system)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug('gui push loop: %s', e)
