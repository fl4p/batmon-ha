# batmon GUI + cross-platform mobile app — architecture plan

Scope: architecture, data model, code structure. **No visual GUI design** — deferred.

## Context

batmon-ha is a headless sampler. It polls one BMS per pack over BLE (or RS485/UART),
normalises into `BmsSample` (`bmslib/bms.py`) and publishes to MQTT with HA discovery
(`bmslib/mqtt_util.py`), plus optional InfluxDB/QuestDB sinks. There is **no UI in the repo**
— `config.yaml` has no `ingress:`/`ports:` and there is no HTTP server anywhere.

Two gaps: non-HA users (`doc/Standalone.md`) get only log lines and MQTT topics; and
multi-pack **topology is invisible** — `bmslib/group.py` merges BMSes into a `group_parallel`
virtual device but flattens it into just another MQTT device tree. `TODO.md` already carries a
`# dashboard` wishlist.

Goal: one GUI showing every pack and its BMS data with topology visible (packs may stay
standalone), shipped twice — a web UI served by the add-on, and a **fully standalone** mobile
app where the phone talks BLE directly to the BMS.

## Decisions taken (user, this session)

| Question | Decision |
|---|---|
| Add-on GUI | Ingress web UI served by batmon itself, also on a plain port standalone |
| Topology | **One level only. No group-of-groups.** |
| Write access | Read-only phase 1; API shape must not preclude writes |
| Mobile background | **Full background logging parity** |
| Mobile role | **Fully standalone** — a phone + a BMS is a complete product, no add-on anywhere |

### The last two decisions force a change of mobile runtime

The original plan ran the drivers under **Pyodide in a webview**. That cannot deliver
background parity: iOS suspends WKWebView JavaScript when the app is backgrounded, so the
decoder sleeps with it. Language is not the issue — the webview is.

**Revised: embed a native CPython interpreter.** CPython 3.13 supports iOS (**PEP 730**) and
Android (**PEP 738**) as official Tier-3 platforms, PyPI serves iOS/Android wheels
(since Feb 2025), and `beeware/Python-Apple-support` builds on the official PEP 730 code.
Embedding Python in an App Store app is established practice.

This is strictly better here, and it *dissolves* most of the Pyodide risk register:

- Runs in the app's real background execution window, not the webview's.
- Full stdlib — `fcntl`/`termios` exist, so the pyserial import wall and the `asyncio.run`
  and `crcmod` problems all evaporate.
- No 13 MB WASM payload, no WKWebView memory ceiling, no cold-start budget.
- **One producer, one transport.** The embedded interpreter runs the *same*
  `bmslib/gui/server.py` on `127.0.0.1`, and the SPA connects by WebSocket exactly as it does
  to the add-on. The mobile app is literally *batmon running on your phone*. The planned
  second serializer, second transport and dual-producer schema risk all disappear.

**Cost, stated honestly:** more native code than Capacitor+Pyodide, and **Capacitor is no
longer the right shell.** With an embedded interpreter and background BLE, the host should be
a thin native app (Swift / Kotlin) that owns CoreBluetooth/Android GATT, embeds CPython, and
hosts a WKWebView/WebView for the SPA — packaged with BeeWare's Briefcase or by hand. My
earlier Capacitor recommendation was right for a foreground-only viewer; that scope changed.

**The real ceiling is iOS, and it binds every app equally.** With the `bluetooth-central`
background mode plus Core Bluetooth **State Preservation and Restoration**, all Core Bluetooth
callbacks fire in the background and iOS will relaunch a terminated app to deliver BLE events.
That is the mechanism for parity — but there is still no guaranteed background *timer*. A
notifying BMS drives near-continuous logging; a polled BMS depends on chaining writes inside
wake windows. No app in any language gets a guaranteed 1 Hz background sample on iOS. Android,
with a foreground service, genuinely can. Parity with *native apps* is achievable; parity
*between the two platforms* is not, and the product should say so.

---

## Independent review

A codex agent reviewed the previous draft with live network and browser access (verified: live
PyPI JSON queries + a browser-fetched HA docs title). Its corrections are folded in below.
Confirmed items are marked ✅, refuted ❌.

❌ **My aiohttp premise was wrong.** Every compiled aiohttp dependency (`multidict`, `yarl`,
`frozenlist`, `propcache`) *does* publish current musllinux armv7l wheels; `aiosignal`/`attrs`
are pure Python. But **aiohttp itself lacks current i686 and armv6l wheels**, and `i386` is in
the arch list — so neither option is free. Decide by **building all five images**, not by
argument. `AIOHTTP_NO_EXTENSIONS=1` is a compiler-free route worth testing. If hand-rolled
framing is chosen anyway, RFC 6455 conformance (masking rejection, continuation sequencing,
fragmented UTF-8, interleaved control frames, control-frame size limits, ping/pong payloads,
close handshake) is a **requirement, not a refinement** — budget conformance tests or use a
library.

✅ **Ingress + `host_network: true` works** — Supervisor builds the destination from
`app.ip_address:ingress_port`, and a host-network app returns the Docker **gateway** address,
not a nonexistent container IP. My top risk was unfounded; drop it.

❌ **But my ingress access control was security theatre.** `X-Ingress-Path` is a request header
any LAN client can set, and **Supervisor ignores port mappings under host networking**, so
`ports: {8099/tcp: null}` does *not* prevent exposure. Must validate the actual socket peer
against Supervisor's ingress source, and authenticate direct access separately.

❌ **Pyodide version scepticism was misplaced.** `314.0.7` is a real release and the byte counts
(WASM 9,598,218 B, stdlib 2,545,637 B, MJS 1,250,344 B = 13,394,199 B; 6,757,104 B compressed)
were verified exact. I wrongly flagged them as fabricated. Moot now that Pyodide is out, but
worth recording.

Also confirmed: no additional module-level import wall in the native drivers (pandas is
confined to `cache/disk.py`, BM6's AES is pure Python, `util`'s threading imports are
function-local); the CRC-16/MODBUS equivalence is mathematically sound (`0x18005` reflected →
`0xA001`); both local bleak-shaped precedents exist; the `main.py:382` sort and the MQTT
watchdog bugs are real. Correction: all-NaN capacity yields **NaN, not `ZeroDivisionError`** —
it is *zero total* capacity that raises.

---

## Phase 0 — bug fixes + shared core

1. **Fix two pre-existing bugs**, separate commit:
   - `main.py:382` — `sorted(sampler_list, key=lambda s: bms.is_virtual)` ignores `s` and closes
     over the leaked loop variable, so the key is constant and "move groups to the end" has
     **never worked**. Matters once `group_serial` exists. → `key=lambda s: s.bms.is_virtual`.
   - **Watchdog suicide without MQTT**: `bg_checks` exits when `mqtt_last_publish_time()` is
     stale, but `mqtt_single_out` returns early when the client is `None` and never sets it.
     Nobody runs without a broker today — **the GUI creates exactly that user.**
2. **`bmslib/wire/`** — new package, importable with **only** stdlib + `bmslib.bms`.
   - `fields.py` ← `sample_desc`, the `meters` dict, `round_to_n`, `capitalize_words`,
     `is_none_or_nan`, `balancing_cells_str`; plus `cell_stats()` extracted from
     `publish_cell_voltages()` and `field_catalog()`.
   - `aggregate.py` ← `sum_parallel`, `is_finite`, `finite_or_fallback`; plus `sum_series`.
   - `model.py`, `topology.py` — serializers.
   - `mqtt_util.py`/`group.py` **re-export** every moved name so MQTT topics and all existing
     importers are untouched.
3. **Portability guard** `test_wire_imports.py` — import every `bmslib.wire.*` in a
   **subprocess** with `paho`/`bleak`/`serial`/`aiohttp`/etc. blocked via `sys.meta_path`.
   Subprocess is mandatory; another test's imports would mask the failure.
4. **Hygiene, now optional rather than critical:** move `SerialServiceStub`/`SerialCharStub`
   **out of the `wired` package** (e.g. `bmslib/gatt_stubs.py`). Note the review's catch: the
   previous plan's `from .wired.stubs import …` would still execute `wired/__init__.py` and
   therefore still import pyserial — Python runs a package's `__init__` before any submodule.
   Native CPython has `termios`/`fcntl` so this no longer blocks mobile, but the decoupling is
   still correct and keeps a browser target possible later.

---

## Phase 1 — topology (one level)

**Extend groups; no `topology:` block.** A group is already a `BmsSampler` with its own MQTT
tree, HA device, meters and switch fan-out. **Schema changes required: none** — `type` and
`address` are already `str`.

1. `bmslib/group.py`: `VirtualGroupBms.KIND='parallel'`; `SeriesGroupBms(VirtualGroupBms)` with
   `KIND='series'`. `BmsGroup.fetch()` dispatches, and **raises `GroupNotReady` on a partial
   member set** (today only `fetch_voltages()` does, so a group can publish a half-sum — a bug
   fix that changes MQTT output for existing users → CHANGELOG).
2. `bmslib/models/__init__.py:53` — uncomment `group_serial`.
3. **Depth-1 guard** in `main.py`'s wiring loop: if a resolved member `is_virtual`, raise naming
   both groups. This one `if` *is* the depth cap.
4. Standalone packs need no config — no edge, appears in `roots`.

### `sum_series` — corrected after review

| field | rule |
|---|---|
| `voltage` | sum |
| `current` | mean (series members carry the *same* current; summing is an N× error) |
| `power` | **derive from aggregates: `sum(V) × mean(I)`** |
| `charge`/`capacity`/`soc`/`soh`/`aged_capacity` | all four from **one** limiting member, selected by **minimum remaining charge** |
| `total_charge_throughput`, `total_charge_net`, `num_cycles` | mean |
| `temperatures` | concat | 
| `mos_temperature` | max of finite |
| `switches` | AND **over known values only**; unknown ⇒ unknown, never `False` |
| `problem` | any() **over known values only**; all-unknown ⇒ `None` |
| `problem_code` | omit (BMS-specific bitmasks; OR-ing is nonsense) |
| `balance_current`, `balancing_cells` | nan / None (internal to a pack) |
| `runtime` | min of finite |
| `timestamp`, `uptime` | min |

Four corrections the review forced, each with a concrete counterexample:

- **Power was arithmetically wrong.** `Σ(Vᵢ·Iᵢ) ≠ ΣVᵢ · mean(I)`. For (12 V, 10 A) and
  (24 V, 12 A): **408 W vs 396 W**. Since mean current is the chosen string-current estimate,
  aggregate power must be derived from the aggregates. Keep summed measured power as a separate
  diagnostic if useful, but do not label it `power`.
- **`min(soc)` selects the wrong pack.** 100 Ah @ 20 % has 20 Ah left; 10 Ah @ 80 % has 8 Ah.
  The second limits discharge despite the higher SoC. Select by **minimum remaining charge**.
  Charging is limited by minimum *headroom* (`capacity − charge`) instead — if a charge-side
  limit is ever surfaced, compute it separately rather than reusing this one.
- **NaN makes `min(key=…)` order-dependent**, and an all-NaN input selects arbitrarily. Define
  the unknown case explicitly and return an explicitly-unknown aggregate rather than a silent
  pick.
- **The "constructor no-op" claim is false.** Reproduced: `charge=1, soc=33, capacity=nan`
  yields `capacity=3` and integer `soc=33`; re-constructing from that sample changes soc to
  **33.33**. A test must pin actual behaviour rather than assert a no-op.

Also: **tri-state is load-bearing.** `any()` over unknown alarms reports "no problem", and
`all()` over unreported switches claims the string conducts. Missing telemetry must not become
reassuring output. Decide single-member-group semantics explicitly (identity vs documented
information loss) — today it would silently discard `problem_code` and balancing data.

Leave `sum_parallel`'s last-writer-wins `switches` merge alone for now (behaviour change on a
shipped feature) but file it; series uses AND from day one, so the two differ until resolved.
Harden the `soc`/`soh` weighted means against **zero** total capacity (the actual
`ZeroDivisionError` trigger), and propagate `problem` — a group currently **hides its members'
alarms**.

---

## Phase 2 — wire schema

Envelope `{"v":1,"type":…,"ts":…,"data":{…}}`; additive-only within a major version.

**`system`**: `producer`, `app_version`, `runtime`, `config_hash`, `fields`, `nodes[]`,
`edges[]`, `roots[]`. `edges[]` is normative (`{parent,child,kind,index}`); `roots[]` is derived
but emitted, with equality validated. Depth-1 invariants enforced in `topology.build()`.

**`state`**: `partial` + `nodes:{id:{link, sample_ts, age_s, stale, values, cells_mv,
cell_stats, temperatures_c, switches, balancing_cells, problem, problem_code,
battery_charging, battery_mode, meters}}`.

**Naming:** keys in `values` are exactly `BmsSample` attribute names in `BmsSample` units;
anything else carries a unit suffix (`cells_mv`, `temperatures_c`, `age_s`). Cell voltages stay
integer millivolts.

**Unknown encoding:** unknown scalar ⇒ **key absent**; unknown inside a positional array ⇒
`null` (the index *is* the cell number). Encode with `json.dumps(..., allow_nan=False)` so a
leaked NaN fails a test instead of breaking `JSON.parse`. ✅ Reviewed as coherent, with one
condition: a state frame must mean **whole-node replacement**, not a merge into `values` —
an absent field clears the previous value and charts record a gap at `sample_ts`. Document that.

**Field catalog** from `sample_desc` re-keyed by `field`. ✅ Verified by AST: **16 entries, 16
unique `field` values, no missing keys** — no collisions. ❌ But **11 of 16 lack
`significant_digits`**, so the planned direct indexing raises `KeyError` on ordinary fields like
capacity. Reproduce `publish_sample`'s `.get('significant_digits', 5)`, and keep explicit topic
metadata when deriving the catalog.

**Five things that must be settled before freezing v1** (review finding 8):

1. `config_hash` currently hashes a document containing `config_hash` — exclude itself and
   define a canonical serialization.
2. **`bms.supports_set_soc()` is called unconditionally, but `VirtualGroupBms` does not define
   it** — system generation would crash on the very groups this feature adds. Use `getattr`
   with a default, and add the method to the group class.
3. Command correlation, acknowledgement and error semantics.
4. Node **removal**, and state/system revision synchronisation.
5. `id = bms.name` makes rename part of identity — define migration.

Freeze only after a minimal SPA consumer and a mobile producer have both exercised it. These
are far likelier breaking changes than adding a scalar.

---

## Phase 3 — server

`GuiStateSink(BmsSampleSink)` for data + a read-only `BmsSampler.status()` for health
(`connected`, `num_samples`, `num_errors`, `t_next_retry`, `last_error`, `device_info`,
`is_virtual`, `debug_data`, `expire_after`), capturing `summarize_exc(ex)` in the existing
`except` branches — that text is currently logged and discarded.

**Link status**, closed enum in precedence order: `never`, `waiting` (GroupNotReady, detail
`bms.debug_data()`), `error`, `connecting` ("retry in 38 s"), `disconnected`, `stale`,
`connected`, plus flat `stale`/`age_s`.

**Acquisition vs delivery must be separated** (review finding 7). The real gates are
`sampling.py:427` (temperatures, when `self.sinks or self.bms_group`) and `sampling.py:526`
(cell voltages, when `self.sinks`). A single `wants_voltages_every_sample` flag covers only the
second, and simply skipping the 526 block would remove the **only** sink voltage-delivery site.
Schedule acquisition independently of delivery, and deliver already-acquired cells to every
interested sink.

**Backpressure — corrected.** Dropping a partial frame is **not** last-value-wins: if one frame
updates pack A and later frames update B and C, dropping A's frame strands A, possibly forever.
Correct options: coalesce pending updates **by node**, or replace an overflowed queue with a
**complete snapshot**, or disconnect and require a fresh snapshot. Define revision/sequence
handling and guarantee `system`-before-`state` ordering; a dropped `system` frame otherwise
leaves a client interpreting state against obsolete topology. `lagged` needs a defined
resynchronisation contract.

**Server library:** unresolved by design. Build all five images both ways in Phase 0 and pick on
evidence (see the review section). Keep `state.py` free of any HTTP dependency so either is a
drop-in.

**Ingress** — `ingress: true`, `ingress_port`, `panel_icon`, `panel_title`. ✅ Works under
`host_network: true` (Supervisor uses the Docker gateway address). Read the Supervisor-selected
port rather than assuming; `ingress_port: 0` with API discovery is supported.

**Security, rewritten.** `X-Ingress-Path` is spoofable and Supervisor **ignores port mappings
under host networking**, so the port is LAN-reachable regardless of `ports:`. Validate the
socket peer against Supervisor's ingress source; authenticate direct access separately with a
token. Phase 1 is read-only, which limits the damage to disclosure of battery state and
identifiers — but **writes must not ship before real auth exists.**

**SPA bundle** lives in `gui/` (source, not shipped) built to `bmslib/gui/web/` (**committed**),
so the Dockerfile's `COPY . .` ships it with zero Dockerfile change and no Node in the image.
CI fails on `git diff --exit-code bmslib/gui/web/`.

---

## Phase 4 — mobile (embedded CPython)

**Step 1 — spike, before any UI.** Build a minimal native app per platform that embeds CPython
3.13 (`Python-Apple-support` / Briefcase on iOS; PEP 738 build on Android), imports `bmslib`,
and drives one **JBD** over a native GATT shim registered through the existing `client_factory`
seam in `bt.py:495` (three lines, mirroring the four backend swaps the repo already does).
Then repeat with **JK**, which exercises `get_service`, `find_char`, the handle branch and PSK
at once. Measure: app size delta, interpreter start time, memory, and background behaviour.

Note on JK: `find_char` matches on **property**, not only handle
(`'notify' in char.properties`), so `jikong.py:313` already disambiguates two characteristics
sharing UUID `0xFFE1` — provided the shim surfaces both instances. Going native removes the
plugin-API concern entirely, since CoreBluetooth and Android both hand you the full
characteristic array. (A JK iOS app exists in the wild, so the platform is proven.)

**Step 2 — background.** iOS: `bluetooth-central` background mode + Core Bluetooth **State
Preservation and Restoration** (`CBCentralManagerOptionRestoreIdentifierKey`), so callbacks fire
in the background and iOS relaunches the app to deliver BLE events. Android: foreground service.
Design the sampler loop as **notification-driven**, not timer-driven, so it survives iOS's lack
of a background timer.

**Step 3 — same server, same SPA.** The embedded interpreter runs `bmslib/gui/server.py` bound
to `127.0.0.1`; the webview loads the committed SPA and connects over WebSocket. One producer,
one transport, no second serializer.

**Step 4 — identity.** iOS exposes an opaque `CBPeripheral.identifier`, never a MAC; Android
gives the MAC. Persist `{id: <our uuid4>, platform_id, adv_name, display_name, type_slug,
service_uuids[], last_seen}` — **`id` is identity, `platform_id` is a cache** — and re-find by
`retrievePeripherals`, falling back to a scan matched on name + advertised service UUIDs, then
rewriting `platform_id`. This is the same lesson as `bt.py:414`/#399: identity is not an address
spelling. Manufacturer data *is* available on iOS, so the auto-detect matchers lifted from
`docs/BMS_FACTSHEET.md` (Basen `0x6F80`, Renogy `0xE14C`, LiTime `0x585A`, Topband) still work.

**Step 5 — scope.** Ship the ~11 native BLE families first; `aiobmsble` is reachable later since
native CPython can simply `pip install` it with the mobile wheels PyPI now serves — a further
gain over Pyodide. Exclude `*_uart` and `dummy` (thread at `dummy.py:164`).

---

## Risks

| Risk | Mitigation |
|---|---|
| **Direct-port exposure bypasses ingress auth** | Peer validation + token; no writes before auth |
| **Server library choice unresolved** | Build all five images both ways in Phase 0; aiohttp lacks i686/armv6l wheels, so neither is free |
| Hand-rolled RFC 6455 if chosen | Explicit conformance tests; bounded HTTP/frame parsing |
| Series aggregation semantics | Corrected above; pin constructor behaviour with tests; tri-state preserved |
| Schema frozen too early | Exercise a real SPA consumer + mobile producer first; settle the five items |
| Embedded CPython app size / App Store | Established practice (BeeWare); measure in the spike |
| **iOS background is event-driven, not timed** | Notification-driven sampler; state restoration; document that iOS ≠ Android fidelity |
| Group `fetch()` now raising changes MQTT output | Bug fix; CHANGELOG |

## Verification

- **Phase 0/1**: `python3 -m pytest bmslib/test -q` unchanged (40 files). New:
  `test_wire_imports.py` (subprocess, blocked modules), `test_wire_sample.py` (no NaN,
  `allow_nan=False`, values bit-identical to `publish_sample`), `test_wire_fields.py` (catalog ↔
  `sample_desc` both ways, **and the missing-`significant_digits` default**),
  `test_topology.py`, `test_group_series.py` (power-from-aggregates incl. the 408-vs-396 case,
  limiting member by remaining charge incl. the 100 Ah@20 % vs 10 Ah@80 % case, NaN handling,
  tri-state switches/alarms, constructor behaviour pinned).
- **Phase 3**: backpressure test proving a dropped update cannot strand a node; ingress-header
  spoof test returning 403; then standalone on this Mac against `type: dummy` (`address:
  test_*`, no hardware) with a parallel group, a series group and a standalone pack; then
  havan.local via `test_and_deploy.sh` for the real ingress path. **Verify `DummyBt` actually
  varies its samples first.**
- **Phase 4**: spike decodes a real JBD then a real JK, on Android and iPhone; background
  behaviour measured over ≥1 h with the screen off on both.

## Out of scope

Visual design, writes (switch/SoC), config editing (under HA that must go through
`POST http://supervisor/apps/self/options`, never a direct `/data/options.json` write),
app-store publishing, historical charting, impedance/qmax views.

## Note

On approval, copy this plan to `plans/gui-and-mobile-app.md` in the repo as step 1.
Full review transcript: `~/codex-reviews/batmon-gui-plan/review.log`.
