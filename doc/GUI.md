# Web GUI

Batmon serves its own web UI. It shows every battery pack, its BMS data, and — when
packs are grouped — the topology they form.

## Home Assistant

The add-on registers an ingress panel, so it appears in the sidebar as **Batmon**.
Nothing to configure. Ingress works even though the add-on runs with
`host_network: true`: the Supervisor builds the destination from the add-on's
`ip_address`, and a host-network add-on reports the Docker network gateway.

## Standalone / Docker

Open `http://<host>:8099/`. Change the port with `gui_port`. With Docker, publish it:

```
docker run ... -p 8099:8099 ghcr.io/fl4p/batmon-ha
```

## Options

| option | default | meaning |
|---|---|---|
| `gui` | `true` | serve the UI at all |
| `gui_port` | `8099` | standalone port. **Ignored under Home Assistant**, where the Supervisor proxies to `ingress_port` in `config.yaml` — changing only `gui_port` there would break ingress. The effective port is logged at startup. |
| `gui_push_period` | `publish_period` | seconds between WebSocket pushes |
| `gui_allow_direct` | `false` under HA, `true` standalone | allow non-ingress connections |

## Security

Ingress requests are authenticated by Home Assistant. A direct connection to the port
is **not**. `X-Ingress-Path` is a request header any client can set, so it is never
used as a credential — the only real control is `gui_allow_direct`, which defaults to
false under Home Assistant.

The WebSocket handshake validates `Origin`. Browsers do **not** apply the
same-origin policy to WebSockets, so without that check any page you visited could
open `ws://<host>:8099/ws` and read your battery data. Requests with no `Origin`
(curl, scripts) are allowed — they are not subject to cross-site hijacking. Ingress
requests are allowed even though the origin differs, because a page cannot set
custom headers on a WebSocket handshake.

Note that with `host_network: true` the socket is on the host regardless of any
`ports:` mapping, so `gui_allow_direct: true` exposes read-only battery state to the
whole LAN. The UI is read-only today; **write support will not ship without
authentication on the direct path.**

## API

Two documents, same shape over REST and WebSocket, enveloped as
`{"v":1,"type":...,"ts":...,"data":{...}}`:

- `GET /api/system` — nodes, `edges`, `roots`, and a field catalog (unit, precision,
  label, icon) so a client hardcodes no unit table.
- `GET /api/state` — a full snapshot. `GET /api/node/<id>` for one node.
- `GET /ws` — `system`, then a full `state`, then partial `state` frames on change.
- `GET /api/health` — liveness, also usable as the add-on watchdog.
- `POST /api/command` — reserved; currently `501`.

Conventions worth knowing before writing a client:

- An unknown **scalar** is an **absent key**, never `null`. Unknown inside a
  positional **array** (`cells_mv`, `temperatures_c`) is `null`, because the index is
  the cell or sensor number. That is the only `null` you will see.
- A `state` frame is **whole-node replacement**, not a merge. An absent field means
  "not reported now"; merging would resurrect a stale reading.
- Numbers are JSON numbers, rounded to the same significant digits as the MQTT values.
- `edges` is normative; `roots` is derived and emitted for convenience.
- Topology is **one level deep** — a group holds packs, never other groups.

The schema is **provisional** until a second independent client has exercised it.

## Trying it without hardware

`doc/options.json.gui-demo` configures five simulated packs, one parallel group, one
series group and one standalone pack — no BLE, no broker:

```
cp doc/options.json.gui-demo options.json
python3 main.py
```


---

# Android app for a JK BMS

`/jk.html` talks to a JK BMS **directly from Android Chrome over Web Bluetooth**.
No add-on, no server, no MQTT — the phone is the BLE client. Open it, tap
**Connect**, pick your JK, and you get pack voltage/current/power/SOC, per-cell
voltages with the low and high cell marked, temperatures, SOH, cycles and the
MOSFET states.

It is a single self-contained file. You can also just copy
`bmslib/gui/web/jk.html` anywhere you can serve it from.

## The one catch: Chrome needs a secure context

Web Bluetooth is refused on a plain `http://` origin, so `http://<batmon-ip>:8099`
will not work as-is — the page says so at the top rather than failing at the tap.
`localhost` and `https://` are both fine. Three ways, easiest first:

**1. Allow the origin in Chrome (no cables, no certs).**
On the phone open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add
`http://<batmon-ip>:8099`, set it to *Enabled*, and relaunch Chrome. Then open
`http://<batmon-ip>:8099/jk.html`.

**2. USB, and use localhost.** With the phone plugged into a machine that has adb:

```
adb reverse tcp:8099 tcp:8099
```

then open `http://localhost:8099/jk.html` on the phone. `localhost` is a secure
context, so nothing else is needed.

**3. Serve it over HTTPS** from anywhere you like — the file is standalone and
needs no batmon instance at all.

## Scope

Read-only, matching the rest of the GUI. The JK protocol supports switching the
charge/discharge/balance MOSFETs (`bmslib/models/jikong.py:set_switch`, addresses
0x1D/0x1E/0x1F) and that is a small addition, but writes are deliberately not
shipped before the auth story is settled.

Only JK is implemented. The decoder is a hand port of `bmslib/models/jikong.py`
covering the 11.x/32S frame layout.

## Keeping the two decoders honest

`bmslib/test/test_jk_webapp_parity.py` runs the JavaScript (under node) and the
Python driver against the **same captured frames** and compares every field,
the cell voltages, the command bytes, and the reassembly of a 300-byte response
from 20-byte notify packets. Two implementations of one wire format drift
silently — a firmware offset fixed on one side only yields plausible wrong
numbers, not an error — so the test fails the build instead. It skips when node
is not installed.
