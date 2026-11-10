## ACCESS

- `gh issue view 41 … --comments | tail -20` succeeded. One reporter’s BM200 works with `bm6` for voltage, SOC and temperature; `bm2` reads nothing. The report is dated November 7. [Issue #41](https://github.com/fl4p/batmon-ha/issues/41)
- Scratch-browser title: “GitHub - KrystianD/bm2-battery-monitor: Python connector and ESPHome template for Bluetooth based Battery Monitors - reverse engineering. · GitHub”. No challenge appeared.
- #412 is a pull request, not an issue. Its description reports hardware verification on two Battery Guard devices. [PR #412](https://github.com/fl4p/batmon-ha/pull/412)

Reviewed all three stats, then diffs, at HEAD `c066066`. **47 tests passed.** Additional reproductions used `/tmp/codex-review-bm41/`; no source files changed.

## FINDINGS

**1. P1 — (c) BM2 can be confirmed from overlapping windows in one foreign notification.**  
[bmslib/auto_detect.py:176](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:176)

The validator counts matching offsets, not distinct frames or notifications. This single **31-byte** notification makes production `detect()` return `bm2`:

```text
454e136cdaea20ce006191b04654aeeaeeb281274e29aa9df16bf2ca210494
```

Matching windows begin at **0 and 13**, overlapping by three bytes. No BM2 device or second notification is needed.

For random ciphertext, each window matches with:

```text
p = (1/256) × (101/256) = 101/65536
```

Two selected independent windows give `p² ≈ 0.000002375`. But 256 bytes produce **241 windows**, giving the ideal-cipher approximation:

```text
P(at least two) = 1 − (1−p)^241 − 241p(1−p)^240
               ≈ 5.393%
```

A seeded simulation produced **545 matches in 10,000 buffers**. Repeated ciphertext creates correlations, so this estimate is not a universal traffic model.

**Fix:** preserve notification/frame boundaries and require stronger independent protocol evidence. Merely requiring nonoverlapping windows still leaves a weak classifier.

**2. P2 — (c) The cap permanently hides valid late replies.**  
[bmslib/auto_detect.py:156](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:156)

The code repeatedly examines the **first** 256 bytes. Once nonmatching traffic fills that prefix, subsequent valid replies cannot recover detection.

Reproduced:

- Two valid BM2 frames: accepted alone, rejected after 256 zero bytes.
- Valid BM6 reply and exact sentinel: same result.
- Full BM6 detection receiving that prefix followed by a valid reply: returns no type.

Clearing before each step does not protect against queued traffic delivered afterward, including during the awaited write. Actual prevalence on BM hardware remains unmeasured; the deterministic failure is established.

**Fix:** process incoming frames incrementally with bounded work and retained framing state, rather than freezing the searchable prefix.

**3. P2 — (c) The random-flood test fails nondeterministically and can fail for the wrong reason.**  
[bmslib/test/test_auto_detect.py:440](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_auto_detect.py:440)

`os.urandom(20000)` followed by “neither validator accepts” has approximately the **5.39% BM2 failure probability above**, even with the cap working. A deterministic sampled counterexample fails the current test.

Removing the cap initially failed at the classification assertion, **before** testing the work bound. With deterministic zero input, removing the cap correctly fails the intended assertion: **39,970 decrypt calls versus 482 allowed**.

**Fix:** separate deterministic work-bound testing from classification tests, and retain adversarial false-positive fixtures.

**4. P2 — (a) BM6’s “cannot be chance” claim is mathematically false.**  
[bmslib/auto_detect.py:168](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:168)

Only three decrypted bytes are fixed. AES does not provide authentication here. Excluding the exact request barely changes the chance probability:

```text
241 / 2^24 ≈ 0.001436% ≈ 1 in 69,615 random 256-byte buffers
```

This is substantially stronger than BM2, but remains probabilistic confirmation of potentially foreign data. Tighten the evidence requirement and remove the categorical comment.

## SURVIVED

- **Existing protocols:** no introduced regression found in Daly, Daly2, JBD, JK or ANT confirmation. Their validators remain unchanged; fixture isolation and request-echo rejection tests pass.

- **GATT gate — qualified:** `GATT_416` rejects both BM probes because it lacks **FFF4**, despite exposing FFF3. However, UUID/property matching does not establish identity. A synthetic BM6 device also exposing FFF1/FFF2 receives three foreign Daly requests before BM6 detection. This is the explicitly documented residual risk, not demonstrated hardware damage. [auto_detect.py:368](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:368)

- **BM2 receiving a BM6 request:** decrypting that request under BM2’s key yields `4ec549eb02a7c64fda9fa3971e424736`. Neither inspected source nor this review establishes how BM2 firmware handles that command. Do not assume it ignores it. The passive probe avoids writes only when it successfully confirms BM2.

- **Passive lifecycle — (b), timing assumption:** `frame=None` performs no write; `response` is irrelevant. Normal exits unsubscribe. But clearing **after** `start_notify()` discards notifications delivered during subscription. Two immediate pushes were lost in a reproduction; `_PushingDevice`’s `sleep(0)` avoids that case. A continuously pushing BM2 normally supplies later frames. [auto_detect.py:341](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:341)

- **Sentinel:** full synthetic detection accepts the exact sentinel. This is consistent with identifying the protocol while `Bm6Bt` rejects the sentinel as measurement data and retries with write-before-notify. The ordering behavior is supported by [PR #412](https://github.com/fl4p/batmon-ha/pull/412).

- **Cache/performance:** `(key, block)` is the correct cache key; `maxsize=1024` bounds entries. Measured cold 241-window scans took about **54–56 ms**, warm scans **0.034 ms**. The cap bounds AES work, not all event-loop work: the receive buffer still grows, `bytes(self._rx_buf)` copies it, and older validators scan unrestricted buffers. [auto_detect.py:352](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:352)

- **Timeout — (b), limited budgeting assumption:** `DETECT_TIMEOUT` is **133 seconds**, enough for nominal silent FFF1–FFF4 probing with prompt BLE operations. It is not a true worst-case bound: all 11 eligible probes can consume 88 seconds waiting plus 55 seconds unsubscribing, before connection overhead. Scanner retries also exceed the simple connection allowance. This broader limitation predates these changes; timeout still fails closed.

- **Timeout hint:** ordinary `TimeoutError` is re-raised; cancellation propagated as `CancelledError` in the supplied Python environment. The warning repeats on every timed-out fetch—there is no throttling. No additional behavior regression found. [bm6.py:368](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/bm6.py:368)

- **Documentation:** recommending `bm6` for the reported BM200 is supported. Universal BM200 compatibility and automatic identification on actual BM200 hardware are not established by one report or the generic synthetic fixture. README’s “never guessed” promise is contradicted by finding 1.

- **Known-bad calibration:** reverting probe code in memory breaks the new positive detection tests; reverting the hint breaks its assertion. The cap test detects removed bounding with deterministic input. Bypassing the cache usefully measures decrypt-call bounds, but does not test cache effectiveness.
