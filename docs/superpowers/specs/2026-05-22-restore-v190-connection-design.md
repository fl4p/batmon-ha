# Restore v1.90 connection model (keep aiobmsble + bleak 2.0)

Date: 2026-05-22
Branch: `restore-v190-connection` (off `bt-backends`)

## Problem

v1.90 is the most stable batmon-ha release to date. The releases after it
(v1.91, v1.92, and the current `bt-backends` work) introduced three large,
intertwined BLE changes at once:

1. Swapped the vendored `bms_ble` decoders for the external `aiobmsble` package.
2. Replaced per-device ephemeral BLE scanning with a long-lived **shared
   scanner** (`bmslib/scan.py`).
3. Upgraded bleak `1.1.0 → 1.1.1 → 2.0.0`.

The decoder swap is a non-factor for stability — the `bms_ble` decoders were
copy-pasted from `aiobmsble`, so they are equivalent code. The suspect changes
are the **shared scanner** and the rewritten **connection code**.

## Goal

Isolate the scanner/connection changes as the stability variable: revert the
connection code and the shared scanner to v1.90's ephemeral-per-device model,
while keeping everything else current (aiobmsble, bleak 2.0.0). This tests
whether the connection rewrite — independent of the bleak version — is the
regression.

## Decisions (confirmed with user)

- **Keep** aiobmsble (current pip-from-git) and its constructor API
  (`keep_alive=`, async `device_info()`).
- **Keep** bleak `2.0.0` pinned as-is. Do **not** restore the 1.1.0 pin.
- **Revert** only the connection code and the shared scanner to v1.90.
- **Revert fidelity:** restore v1.90's ephemeral-scanner connection logic but
  keep two harmless post-1.90 niceties — the RSSI columns in `bt_discovery`
  and the `ConnectLock` that serializes connects.
- **`scan.py`:** leave dormant (not deleted) — see below.

## Known risk

The shared scanner was likely introduced *because* bleak 2.0 / BlueZ misbehaves
with rapid start/stop of multiple scanners (hence `restart=True` and the
`"[org.bluez.Error.InProgress] Operation already in progress"` workaround in the
current code). Reverting to ephemeral scanners **while staying on bleak 2.0**
may resurface those errors — v1.90 avoided them partly by being on bleak 1.1.0.
This is accepted: isolating one variable at a time is the point. Watch the
havan logs for `InProgress` / scanner-start errors during verification.

## Architecture context

Two connection paths coexist in the current code:

1. **Native models** (`jbd`, `daly`, `daly2`, `ant`, `sok`, `jikong`, `victron`,
   `litime`, `supervolt`, `dummy`) subclass `BtBms` and connect via
   `bmslib/bt.py`'s `_connect_client()`. In v1.90 this connected by address
   string against a `BleakClient` built in `__init__`, with an ephemeral scanner
   only as a fallback. The current code re-resolves the address through the
   shared scanner and rebuilds the client from a `BLEDevice` mid-connect.
2. **aiobmsble-wrapped models** go through `bmslib/models/BLE_BMS_wrap.py`'s
   `BMS`, which — even in v1.90 — delegated the actual connect to the decoder
   library's `BaseBMS._connect()`. The only connection-relevant change here is
   that `BLEDeviceResolver.resolve()` switched from an ephemeral scanner to the
   shared one.

`bt_power()` (per-controller power-cycle, gated by the `bt_power_cycle` config
option in `main.py:134`) is unrelated to the scanner and stays as-is.

## Changes

Reference implementation = v1.90 commit `f3eaeba`. Hand-port the function
bodies via targeted edits (not a wholesale `git checkout`), adapting only where
the aiobmsble API or bleak 2.0 differ. No `requirements.txt` / `Dockerfile`
changes.

### 1. `bmslib/bt.py` — `_connect_client()`
Revert to v1.90: connect the address-based `self.client` (built in `__init__`
via `_create_client`) directly:
```python
await asyncio.wait_for(self.client.connect(timeout=timeout), timeout=timeout + 1)
```
Remove the `resolve_address` / `get_shared_scanner` calls and the mid-connect
client re-creation. Keep the `_create_client()` helper and the existing
`BleakDeviceNotFoundError → bt_discovery` fallback.

### 2. `bmslib/bt.py` — `_connect_with_scanner()`
Revert to v1.90's ephemeral scanner: construct a local `BleakScanner`,
`await scanner.start()`, run the back-off retry loop, and `await scanner.stop()`
on both the success and failure paths. Removes the
`get_shared_scanner(..., restart=True)` + `sleep(1)` version.

### 3. `bmslib/bt.py` — `bt_discovery()` (nicety kept)
Keep the current RSSI table output, but back it with an ephemeral `BleakScanner`
(start → `sleep(timeout)` → read `discovered_devices_and_advertisement_data` →
log table → stop) instead of the shared scanner.

### 4. `bmslib/models/BLE_BMS_wrap.py` — `BLEDeviceResolver.resolve()`
Revert to v1.90's ephemeral scanner (build `BleakScanner`, start, poll
`discovered_devices` for 5s, stop). Everything else in the `BMS` wrapper stays
on the current aiobmsble API and `ConnectLock`. Remove the now-unused
`from bmslib.scan import get_shared_scanner` import.

### 5. `bmslib/scan.py` — dormant
After the four edits, the only remaining importer is `main.py`'s
`stop_all_scanners()` (called at shutdown, `main.py:388`). The `_scanners` dict
is never populated, so `stop_all_scanners()` becomes a harmless no-op. Leave
`scan.py` and the `main.py` call in place — smaller, lower-risk diff, trivially
removable later.

## Untouched

- `requirements.txt` / `Dockerfile`: bleak 2.0.0 and aiobmsble unchanged.
- `ConnectLock` (defined in `bt.py`, used in both wrappers' `__aenter__`): kept.
- `bt_power()` and the `bt_power_cycle` gate in `main.py`: kept.
- All native model decoders and the aiobmsble wrapper body (except the four
  reverted functions above).

## Verification (on havan.local)

`havan.local` (Raspberry Pi, HA Supervised) runs the addon as docker container
`addon_local_batmon` (slug `batmon`), built by the supervisor from
`/var/lib/homeassistant/addons/local/batmon-ha` — a **non-git** directory,
world-writable by user `fab` (no sudo needed to copy files). It currently runs
a 1.90-based build ("1.90e").

Local pre-checks (no BLE hardware here):
- `python -c "import bmslib.bt, bmslib.models.BLE_BMS_wrap"` imports cleanly.
- Dummy/`test_` BMS path still works.

On-device verification:
1. Sync the working tree to `/var/lib/homeassistant/addons/local/batmon-ha/`
   on havan (rsync over SSH; exclude `.git`, `__pycache__`, `docs`).
2. `ha addons rebuild local_batmon` (rebuilds the image from the synced source).
3. `ha addons logs local_batmon` (and/or `sudo docker logs -f addon_local_batmon`)
   — confirm each configured BMS connects and samples, and watch for
   `InProgress` / scanner-start errors (the known risk above).

Success = all configured BMS devices connect and report samples as reliably as
the current 1.90e build, with no new scanner errors over a sustained run.
