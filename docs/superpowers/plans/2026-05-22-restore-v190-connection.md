# Restore v1.90 Connection Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revert batmon-ha's BLE connection code and discovery to v1.90's ephemeral-per-device scanner model, while keeping aiobmsble and bleak 2.0.0 unchanged, to isolate the connection rewrite as the post-1.90 stability regression.

**Architecture:** Four functions are reverted to their v1.90 form (commit `f3eaeba`): three in `bmslib/bt.py` (`bt_discovery`, `_connect_client`, `_connect_with_scanner`) and one in `bmslib/models/BLE_BMS_wrap.py` (`BLEDeviceResolver.resolve`). Each stops calling the shared scanner (`bmslib/scan.py`) and instead constructs a short-lived `BleakScanner` it starts and stops itself. `scan.py` is left in place but becomes dormant (only `main.py`'s shutdown no-op still imports it). No dependency or `Dockerfile` changes.

**Tech Stack:** Python 3 / asyncio, bleak 2.0.0, aiobmsble (pip-from-git), Home Assistant Supervised addon (docker), verified on `havan.local` over SSH.

**Spec:** `docs/superpowers/specs/2026-05-22-restore-v190-connection-design.md`

**Branch:** `restore-v190-connection` (already created off `bt-backends`).

---

## File Structure

- `bmslib/bt.py` — modify: top bleak import (add `BleakScanner`), remove `scan` import, rewrite `bt_discovery` / `_connect_client` / `_connect_with_scanner`.
- `bmslib/models/BLE_BMS_wrap.py` — modify: remove `scan` import, rewrite `BLEDeviceResolver.resolve`.
- `bmslib/scan.py` — untouched (dormant after edits).
- `main.py` — untouched (`stop_all_scanners()` becomes a harmless no-op).

**Pre-flight reminder:** No real BLE hardware is attached to the dev machine, and aiobmsble is not installed locally. Local verification is limited to **syntax compilation** (`python3 -m py_compile`). Behavioral verification happens on `havan.local` (Task 7).

---

### Task 1: Add `BleakScanner` import, drop `scan` import in `bt.py`

**Files:**
- Modify: `bmslib/bt.py:16` and `bmslib/bt.py:21`

- [ ] **Step 1: Add `BleakScanner` to the bleak import**

Replace line 16:
```python
from bleak import BleakClient
```
with:
```python
from bleak import BleakClient, BleakScanner
```

- [ ] **Step 2: Remove the now-obsolete shared-scanner import**

Delete line 21 entirely:
```python
from .scan import resolve_address, get_shared_scanner
```
(Leave the surrounding imports on lines 19-20 and 22-23 intact. `bmslib/scan.py` itself is NOT modified.)

- [ ] **Step 3: Verify the file still compiles**

Run: `python3 -m py_compile bmslib/bt.py`
Expected: no output, exit code 0.

(Do not commit yet — `bt_discovery`, `_connect_client`, and `_connect_with_scanner` still reference the removed names; they are fixed in Tasks 2-4. Commit happens at the end of Task 4.)

---

### Task 2: Revert `bt_discovery()` to an ephemeral scanner (keep RSSI columns)

**Files:**
- Modify: `bmslib/bt.py:40-65`

- [ ] **Step 1: Replace the whole `bt_discovery` function**

Replace the current function (lines 40-65, the `@backoff...` decorator through `return devices`) with:

```python
@backoff.on_exception(backoff.expo, Exception, max_time=10, logger=get_logger())
async def bt_discovery(logger, timeout: int = 5, adapter=None):
    ad = adapter or 'default'
    logger.info('BT Discovery (%d seconds, adapter=%s):', timeout, adapter or 'default')
    scanner = BleakScanner(adapter=adapter) if adapter else BleakScanner()
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
        if hasattr(scanner, 'discovered_devices_and_advertisement_data'):
            devices = scanner.discovered_devices_and_advertisement_data
            addr_len = (max(len(d.address) for d, a in devices.values()) + 1) if devices else 20
            if not devices:
                logger.info(' - no devices found - ')
            else:
                logger.info("%s %*s %26s %4s", ad, addr_len, 'addr', 'name', 'rssi')
            for d, a in sorted(devices.values(), key=lambda t: t[0].address):
                logger.info("%s %*s %26s %4s", ad, addr_len, d.address, d.name, a.rssi)
            return [d for d, a in devices.values()]
        else:
            devices = scanner.discovered_devices
            if not devices:
                logger.info(' - no devices found - ')
            else:
                logger.info("BT %18s %26s", 'addr', 'name')
            for d in devices:
                logger.info("BT %s %26s", d.address, d.name)
            return devices
    finally:
        await scanner.stop()
```

This keeps the current RSSI table output but backs it with a short-lived scanner instead of `get_shared_scanner`. The `try/finally` guarantees the scanner is stopped even on error.

- [ ] **Step 2: Verify the file still compiles**

Run: `python3 -m py_compile bmslib/bt.py`
Expected: no output, exit code 0.

---

### Task 3: Revert `_connect_client()` to direct connect (no re-resolve)

**Files:**
- Modify: `bmslib/bt.py:409-424` (inside `_connect_client`)

- [ ] **Step 1: Remove the shared-scanner resolve + client re-creation**

In `_connect_client`, replace this block (currently lines 409-424, from the blank line after the `verbose_log` log call through the `asyncio.wait_for(...)` line):

```python

        # re-create the client because we use our own addr-to-device resolving (with the shared scanner)
        dev = await resolve_address(self.address, adapter=self._adapter, timeout=timeout)
        if dev is None:
            self.logger.warning('%s: device %s not discovered from adapter %r, trying to connect anyway', self.name, self.address,
                                self._adapter or "default")
            await (await get_shared_scanner(self._adapter)).stop()
            # ^ stop scanner to prevent
            # ^ `bleak.exc.BleakDBusError: [org.bluez.Error.InProgress] Operation already in progress`

        self.client = self._create_client(self.address if dev is None else dev)

        try:
            # bleak's connect timeout is buggy (on macOS), so we wrap another timeout
            # dev = await resolve_address(self.address, self._adapter, timeout=timeout)
            await asyncio.wait_for(self.client.connect(timeout=timeout), timeout=timeout + 2)
```

with:

```python

        try:
            # bleak's connect timeout is buggy (on macOS), so we wrap another timeout
            await asyncio.wait_for(self.client.connect(timeout=timeout), timeout=timeout + 1)
```

This makes `_connect_client` connect the address-based `self.client` already built in `__init__` (`bmslib/bt.py:299`, `self.client = self._create_client(address)`), exactly as v1.90 did. The `except`/`bt_discovery` fallback and everything below (`self._connect_time = ...`, `enumerate_services`, psk pairing) stays unchanged.

- [ ] **Step 2: Verify the file still compiles**

Run: `python3 -m py_compile bmslib/bt.py`
Expected: no output, exit code 0.

---

### Task 4: Revert `_connect_with_scanner()` to an ephemeral scanner, then commit `bt.py`

**Files:**
- Modify: `bmslib/bt.py:495-520` (inside `_connect_with_scanner`)

- [ ] **Step 1: Replace the shared-scanner body with an ephemeral scanner**

Replace this block (currently lines 495-520, from `from bmslib.scan import get_shared_scanner` through the end of the `while` loop):

```python
        from bmslib.scan import get_shared_scanner
        scanner = await get_shared_scanner(self._adapter, restart=True)
        await asyncio.sleep(1)

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                ad = f' using adapter {self._adapter}' if self._adapter else ''
                if self.client.address not in discovered:
                    raise BleakDeviceNotFoundError(
                        self.client.address, 'Device %s%s not discovered. Make sure it in range and is not being '
                                             'accessed by another app. (found %s)' % (
                                                 self.client.address, ad, discovered))

                self.logger.debug("connect attempt %d", attempt)
                await self._connect_client(timeout=timeout / 2)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.debug('retry %d after error %s', attempt, e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    raise
```

with:

```python
        scanner = BleakScanner(adapter=self._adapter) if self._adapter else BleakScanner()
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                ad = f' using adapter {self._adapter}' if self._adapter else ''
                if self.client.address not in discovered:
                    raise BleakDeviceNotFoundError(
                        self.client.address, 'Device %s%s not discovered. Make sure it in range and is not being '
                                             'accessed by another app. (found %s)' % (
                                                 self.client.address, ad, discovered))

                self.logger.debug("connect attempt %d", attempt)
                await self._connect_client(timeout=timeout / 2)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.debug('retry %d after error %s', attempt, e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    await scanner.stop()
                    raise

        await scanner.stop()
```

This restores v1.90: a local scanner started before the retry loop and stopped on both the success path (final line) and the give-up path (inside the `else`).

- [ ] **Step 2: Verify the file compiles and no stale references remain**

Run:
```bash
python3 -m py_compile bmslib/bt.py && grep -n "get_shared_scanner\|resolve_address" bmslib/bt.py
```
Expected: `py_compile` produces no output (exit 0); `grep` prints nothing (no matches) — confirming `bt.py` no longer touches the shared scanner.

- [ ] **Step 3: Commit `bt.py`**

```bash
git add bmslib/bt.py
git commit -m "Revert bt.py connection + discovery to v1.90 ephemeral scanner

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Revert `BLEDeviceResolver.resolve()` and remove the `scan` import in `BLE_BMS_wrap.py`

**Files:**
- Modify: `bmslib/models/BLE_BMS_wrap.py:11` and `bmslib/models/BLE_BMS_wrap.py:28`

- [ ] **Step 1: Remove the shared-scanner import**

Delete line 11 entirely:
```python
from bmslib.scan import get_shared_scanner
```
(Keep line 10 `from bmslib.bt import BtBms, BleakDeviceNotFoundError, ConnectLock` — `ConnectLock` is intentionally retained.)

- [ ] **Step 2: Replace the scanner acquisition with an ephemeral scanner**

In `BLEDeviceResolver.resolve`, replace the single line (currently line 28):
```python
        scanner = await get_shared_scanner(adapter)
```
with:
```python
        import bleak
        scanner_kw = {}
        if adapter:
            scanner_kw['adapter'] = adapter
        scanner = bleak.BleakScanner(**scanner_kw)

        await scanner.start()
```

- [ ] **Step 3: Stop the scanner before returning**

The function currently ends with:
```python
        return BLEDeviceResolver.devices.get(key, None)
```
Replace that single line with:
```python
        await scanner.stop()
        return BLEDeviceResolver.devices.get(key, None)
```
(The 5-second `while` polling loop in between is unchanged.)

- [ ] **Step 4: Verify the file compiles and no stale references remain**

Run:
```bash
python3 -m py_compile bmslib/models/BLE_BMS_wrap.py && grep -n "get_shared_scanner" bmslib/models/BLE_BMS_wrap.py
```
Expected: `py_compile` produces no output (exit 0); `grep` prints nothing.

- [ ] **Step 5: Commit `BLE_BMS_wrap.py`**

```bash
git add bmslib/models/BLE_BMS_wrap.py
git commit -m "Revert BLEDeviceResolver to v1.90 ephemeral scanner

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Whole-repo syntax + import-surface sanity check

**Files:** none (verification only)

- [ ] **Step 1: Compile every touched module and confirm scan.py is dormant**

Run:
```bash
python3 -m py_compile bmslib/bt.py bmslib/models/BLE_BMS_wrap.py main.py
echo "--- remaining scan.py importers (expect only main.py) ---"
grep -rn "from bmslib.scan\|import bmslib.scan\|bmslib\.scan" --include="*.py" .
```
Expected: `py_compile` produces no output; the only `scan` importer printed is `main.py:21` (`from bmslib.scan import stop_all_scanners`). No references in `bt.py` or `BLE_BMS_wrap.py`.

- [ ] **Step 2: Confirm the diff matches the spec scope**

Run: `git diff bt-backends...HEAD --stat`
Expected: exactly two code files changed — `bmslib/bt.py` and `bmslib/models/BLE_BMS_wrap.py` (plus the spec/plan docs committed separately). `requirements.txt`, `Dockerfile`, and `bmslib/scan.py` must NOT appear.

---

### Task 7: Deploy to havan.local and verify on hardware

**Files:** none (deploy + observe)

> **Decision needed before this task:** the v1.90-based addon `local_batmon` (the deploy target, built from `/var/lib/homeassistant/addons/local/batmon-ha`) is currently **stopped**, and the **bumble test** addon `local_batmon_bumble` is running. They likely contend for the same BT adapter. Default plan: stop the bumble addon, deploy into `batmon-ha`, start `local_batmon`. Confirm with the user if a side-by-side slug is preferred instead.

- [ ] **Step 1: Bump the addon version so the supervisor rebuilds the image**

Edit `/var/lib/homeassistant/addons/local/batmon-ha/config.yaml` on havan to a new version tag (e.g. `1.90f-rev`), so `ha addons rebuild` picks up the new source. (Confirm current value first: it was `1.90e`.)

```bash
ssh havan.local 'grep -i "^version" /var/lib/homeassistant/addons/local/batmon-ha/config.yaml'
```

- [ ] **Step 2: Sync the working tree to havan (exclude git/cache/docs)**

Run from the repo root:
```bash
rsync -av --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='docs' \
  --exclude='*.pyc' --exclude='user_id' \
  ./ havan.local:/var/lib/homeassistant/addons/local/batmon-ha/
```
Expected: `bmslib/bt.py` and `bmslib/models/BLE_BMS_wrap.py` listed in the transferred files.

(Then re-apply the version bump from Step 1 if `--delete`/sync overwrote `config.yaml`; alternatively bump the version in the local repo's `config.yaml` before syncing.)

- [ ] **Step 3: Stop the bumble addon (per decision above) and rebuild + start batmon**

```bash
ssh havan.local 'ha addons stop local_batmon_bumble; ha addons rebuild local_batmon; ha addons start local_batmon'
```
Expected: rebuild completes without a Python import/traceback error (this is the real test that aiobmsble + the reverted code import cleanly together).

- [ ] **Step 4: Watch logs and confirm BMS connectivity**

```bash
ssh havan.local 'ha addons logs local_batmon'
```
Observe for a sustained window (several sample cycles):
- Each configured BMS connects and reports samples.
- **Watch for the known risk:** `[org.bluez.Error.InProgress] Operation already in progress` or scanner start/stop errors — these would indicate the ephemeral scanner is fighting bleak 2.0/BlueZ.

- [ ] **Step 5: Record the outcome**

Note in the commit/PR description (or back to the user) whether connections are as reliable as the prior 1.90e build and whether any `InProgress`/scanner errors appeared. If errors appear, that is the signal that the shared scanner was compensating for bleak 2.0 — capture the log lines for the follow-up decision (re-pin bleak to 1.1.0, or reintroduce a minimal shared scanner).

---

## Notes for the implementer

- **Do not** change `requirements.txt` or `Dockerfile` — bleak stays at 2.0.0, aiobmsble stays as the pip-from-git dependency. This is deliberate (isolating one variable).
- **Do not** delete `bmslib/scan.py` or edit `main.py` — leaving `stop_all_scanners()` as a no-op is intentional and keeps the diff minimal.
- The aiobmsble-wrapped `BMS` class in `BLE_BMS_wrap.py` keeps its current constructor call (`keep_alive=...`) and async `device_info()` — only `BLEDeviceResolver.resolve` is reverted.
- `ConnectLock` (in `bt.py`, used in both wrappers' `__aenter__`) is intentionally retained.
