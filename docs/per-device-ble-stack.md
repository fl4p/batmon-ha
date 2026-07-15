# Per-device `ble_stack` selection (scoping / design)

Motivation: issue #385. A user runs a **JK + Daly mix on one host**. No single
global stack serves both:

| stack   | JK  | Daly |
|---------|-----|------|
| `bluek` | ok  | won't connect |
| `bleak` | intermittent | ok |

They need **bleak for the Daly and bluek for the JK at the same time**. Today
`ble_stack` is a single process-wide choice, so that combination is impossible.

## How stack selection works today (why it's global)

Selection happens at three levels, all process-wide:

1. **bumble / bluek** — `addon_main.sh` prepends the shadow package's
   `_shadow/` dir to `PYTHONPATH` *before Python starts*
   (`addon_main.sh:107-140`). That makes `import bleak` — everywhere in the
   process, including inside `aiobmsble` — resolve to `bumble_bleak` / `bluek`.
   There is exactly **one** `bleak` in `sys.modules` for the whole run.
2. **esphome** — runs a **different venv** (`/app/venv_esphome`) with `bleak>=3`,
   which conflicts with the `bleak==2` pin in the main venv. Cannot share a
   process with the other stacks at all.
3. **esphome shim** — `main.py:_early_select_ble_stack()` installs a bleak shim
   inside that venv.

`bmslib/bt.py` then binds the classes once at import
(`from bleak import BleakClient, BleakScanner`, `bt.py:17`) and branches on
`BleakClient.__module__` for stack-specific behaviour
(`bt_stack_version`, `bt_power`, `bt_diagnostics`).

## Key feasibility finding

`bluek` and `bumble_bleak` **each export a bleak-compatible API under their own
package name** and are importable independently of the global shadow:

```python
from bluek import BleakClient, BleakScanner          # bluek/__init__.py
from bumble_bleak import BleakClient, BleakScanner    # bumble_bleak/__init__.py
```

- bluek's core does **not** internally `import bleak`; `import bluek.shadow` (the
  global redirect) is opt-in and separate.
- bluek's constructors are drop-in for `bt.py`'s exact call sites:
  `BleakClient(address, disconnected_callback=, adapter=, handle_pairing=, **kw)`
  and `BleakScanner(adapter=)`.
- bluek talks to the kernel BlueZ stack over sockets and **coexists with
  bluetoothd**, so bleak-for-Daly + bluek-for-JK on the *same adapter* is sound.

⇒ `{bleak, bluek}` (and `bumble` on a *separate* adapter) can coexist in one
process by importing each client class directly and choosing per device.

## Scope

**In scope (tractable, covers #385):** per-device selection among
`bleak` / `bluek` / `bumble` for **native BMS models** (the ones in
`bmslib/models/` that use `BtBms.self.client` — jikong, daly, jbd, ant, sok,
victron, …). Implemented by making the client/scanner **classes per-instance**
instead of a module-global import.

**Out of scope (documented limitations):**

- **esphome per-device** — impossible in-process (separate venv, bleak 3 vs 2).
  Stays global-only.
- **Global shadow + per-device override are mutually exclusive.** If the process
  is launched with a bluek/bumble `_shadow` on `PYTHONPATH`, stock BlueZ `bleak`
  is already gone from the process and cannot be a per-device choice. Therefore
  **per-device selection requires the global stack to be `bleak`** (no shadow);
  individual devices then opt *into* bluek/bumble by direct import. This is the
  natural default and the exact shape #385 needs.
- **aiobmsble-wrapped models** (`BLE_BMS_wrap` / `bms_ble`) construct their own
  bleak client internally via `import bleak`, so without the global shadow they
  always get stock bleak. Per-device stack for those needs client injection —
  deferred. (The #385 devices are native jikong/daly, unaffected.)
- **bumble** takes the adapter exclusively (HCI User Channel), so a bumble device
  must be on its **own adapter**, separate from any bleak/bluek device.

## Design

### Config

Add an optional per-device key, defaulting to the global `ble_stack`:

```yaml
ble_stack: bleak            # global default (must stay bleak to allow overrides)
devices:
  - address: <daly-mac>     # inherits global -> bleak
  - address: <jk-mac>
    ble_stack: bluek        # per-device override
```

Schema (`config.yaml`): add `ble_stack: "list(bleak|bumble|bluek)?"` inside the
device block (note: **no `esphome`** at device level). Validate at load: if any
device overrides the stack, the global stack must be `bleak`, else fail fast with
a clear message.

### Code

1. **`bmslib/bt.py` — resolve classes per instance.** Add a small resolver:

   ```python
   def _resolve_stack(name):
       # returns (BleakClient, BleakScanner) for 'bleak'|'bluek'|'bumble'
   ```

   `BtBms.__init__` takes `ble_stack=None`, stores the resolved pair on the
   instance (`self._BleakClient`, `self._BleakScanner`), and `_create_client` /
   `_connect_with_scanner` use those instead of the module globals.

2. **Stack-identity checks become per-instance.** `bt_stack_version`,
   `bt_power`, `bt_diagnostics` currently branch on the module-global
   `BleakClient.__module__`. Route them through the instance's resolved client
   class (or pass the bms in) so the label/skip logic is correct per device.
   `bt_power` skip for bumble already exists — keep it keyed on the *device's*
   class.

3. **`main.py` construct path** passes each device's `ble_stack` into the BMS
   constructor (`construct_bms` / `bmslib/models/__init__.py`).

4. **`addon_main.sh`** — when per-device overrides are present the global stack
   stays `bleak` (no shadow exported), so the existing shell logic is unchanged;
   only add validation/warning. bluek/bumble packages are already installed in
   the image (`Dockerfile:41,47`).

### Test / calibration

- Unit: `_resolve_stack` returns distinct classes; a device with
  `ble_stack: bluek` builds a `bluek.BleakClient`, default builds `bleak`'s.
- Guard: loading a config with a per-device override **and** a non-bleak global
  stack must **raise**, not silently fall back (absence-of-evidence rule).
- Bench: on a host with bluek installed, JK on bluek + Daly on bleak, same
  adapter, sample both for N minutes.

## Decision & status — Approach A, implemented

Chosen model: **per-device override is only legal when the global
`ble_stack` is `bleak`**; any other global stack + an override is a hard config
error. Matches #385, least surprising, smallest blast radius. (The rejected
alternative was dropping the shadow entirely and making every stack a per-device
import including aiobmsble via client injection — bigger change, deferred.)

Implemented in:

- `config.yaml` — device-level `ble_stack: "list(bleak|bumble|bluek)?"`.
- `bmslib/bt.py` — `_resolve_stack(name)`; `BtBms.__init__(ble_stack=)` stores
  `self._BleakClient/_BleakScanner`; `_create_client` / `_connect_with_scanner`
  use them; `bt_stack_version(client_cls=None)` labels per device.
- `bmslib/models/__init__.py` — `construct_bms` threads `ble_stack`;
  `check_ble_stack_config()` enforces the global-must-be-bleak rule.
- `bmslib/models/BLE_BMS_wrap.py` — aiobmsble models reject a non-bleak override
  loudly (they can't honour it).
- `bmslib/sampling.py` — diagnostics line reports the device's stack.
- `main.py` — calls `check_ble_stack_config` at startup (fail fast).
- Tests: `bmslib/test/test_ble_stack.py` (11 cases).

Missing-package requests (`bluek`/`bumble` not installed) raise a clear
`RuntimeError` — never a silent fallback to bleak.

### Fail-fast coverage (post-review hardening)

Two independent reviews found the first cut's gate was narrower than its
docstring: it only rejected "override + non-bleak global", so under a `bleak`
global any invalid device value (a typo, `esphome`, case/whitespace variants)
slipped through and crashed later, deep in `_resolve_stack`, with an *uncaught*
traceback that took down every configured BMS. Fixed:

- `check_ble_stack_config` now validates **every** per-device value against
  `PER_DEVICE_BLE_STACKS = {bleak, bluek, bumble}` (exact match — typos/`esphome`
  rejected) *before* the global-must-be-bleak rule, so all config-value errors
  fail fast with a clear message. Calibrated by tests against known-bad inputs.
- `main.py` wraps the device-construction loop so a runtime stack failure
  (missing package → `RuntimeError`; aiobmsble override → `NotImplementedError`)
  also exits cleanly instead of dumping a traceback.
- The aiobmsble guard (`BLE_BMS_wrap`) now compares the requested stack against
  the *actual* process stack (`bleak.BleakClient.__module__`), not the literal
  `'bleak'`, so annotating an aiobmsble device with the same stack as a non-bleak
  global (a no-op) is no longer wrongly rejected.
- `_resolve_stack` is skipped for `test_`/serial devices, so a stray `ble_stack`
  copied onto a wired device can't crash its construction.

### Not covered (documented limitations / future work)
- **aiobmsble-wrapped models** per-device (needs client injection) — rejected
  loudly, not supported.
- **`bt_power` + `bumble`**: the global BT power-cycle (`bt_power_cycle`) still
  drives every controller via bluetoothctl and is not per-device aware. bumble
  claims its adapter exclusively (HCI User Channel), so power-cycling can fight
  that claim. `bt_controllers()` reports MACs while device `adapter:` normalizes
  to `hciN`, so a correct per-adapter skip needs a MAC↔hci mapping — deferred;
  for now `main.py` emits a startup **warning** when both are configured. (bluek,
  the #385 case, coexists with bluetoothd and has no such conflict.)
- **Error-path diagnostics** (`bt_diagnostics`/`bt_discovery`) still scan with
  the global bleak scanner, so a per-device bluek/bumble device's *diagnostic*
  logs may be reported via stock bleak. Diagnostic-only (never on the
  connect/reconnect path); labels can mislead but nothing breaks.
- **Bench validation on real hardware**: JK-on-bluek + Daly-on-bleak, one
  adapter — still the one open item before closing #385.
</content>
</invoke>
