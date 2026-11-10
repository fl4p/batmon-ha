## ACCESS

- GitHub API succeeded: `99584aa` — “auto_detect: bound every await and derive the time limit from the bounds”.
- Playwright title: “bm2-battery-monitor/.docs/reverse_engineering.md at master · KrystianD/bm2-battery-monitor · GitHub”. No human challenge encountered; Fab’s browser was not used.

**56 tests passed. Four findings remain.** No tracked repository changes. Reproductions, 12 mutation checks, and the complete await audit are in [review.md](/tmp/codex-review-bm41b/review.md); provenance is in [access.md](/tmp/codex-review-bm41b/access.md). No hardware testing was performed.

## FINDINGS

**1. P2 — (c) The frozen-prefix defect remains after 64 notifications.**  
[auto_detect.py:347](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:347)

`_rx_frames` permanently retains the first 64 notifications. BM validators use that list, regardless of newer bytes retained in `_rx_buf`. Even wrong-length notifications consume slots.

Reproduced with 64 junk notifications followed by a valid BM2/BM6 frame: both validators reject it, although it passes alone. Full `detect()` also fails; the BM2 case proceeds to send a foreign BM6 request.

**Prior finding 2 is not fixed—its threshold moved.** Consume notifications incrementally or retain recent frames with preserved positive evidence. Test positions 64, 65, and beyond.

**2. P2 — (c) Subscription timeout leaks a callback on the pinned ESPHome stack.**  
[auto_detect.py:393](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:393), [cleanup:417](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:417)

The new 10-second timeout can cancel subscription setup before its acknowledgement. In pinned aioesphomeapi 46.3.0, the callback is installed before that await, but cancellation bypasses its `except Exception` cleanup. Neither its registry nor bleak-esphome’s subscription registry has been populated yet, so subsequent `stop_notify()` does nothing.

Executing the actual pinned methods against a fake transport reproduced **one surviving callback, empty registries, no unsubscribe request, and delivery of a late frame after cleanup**.

Moving subscription inside `try` is necessary but insufficient. Fix cancellation rollback before registry insertion and test late delivery/resubscription. No wrong-type confirmation or physical proxy leak was demonstrated.

**3. P2 — (c) Scanner startup remains outside the cleanup guard.**  
[bt.py:697](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:697)

`await scanner.start()` precedes the new `try/finally`. Cancelling there leaves partially started scanners untouched.

Reproduction: scanner marked active, startup suspended, caller cancelled—**zero stop calls**, scanner still active. Bleak’s CoreBluetooth implementation has the relevant structure: register callback/start scanning, then await confirmation. The pinned habluetooth wrapper does not suspend internally during startup, so that specific window does not apply there.

Protect startup and test partial-start cleanup. The supplied test factory replaces `_connect_with_scanner`, so it cannot detect this omission.

**4. P2 — (c) The advertised wall-time guarantee excludes cancellation cleanup.**  
[auto_detect.py:47](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:47), [outer timeout:522](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/auto_detect.py:522)

`wait_for()` waits for cancellation to finish; adding another `wait_for()` does not establish an independent cleanup deadline. [Python documents this explicitly](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for).

Outside `_fast`, a production `_resolve_one()` reproduction configured with a **20 ms outer limit** took approximately **239 ms** while unsubscribe cancellation cleanup finished. Bleak 2.0.0’s BlueZ connect unwinding also awaits a D-Bus disconnect response without a separate timeout.

The arithmetic is reasonable **under prompt cancellation**, but “holds by construction” is unsupported. Establish a bounded cleanup policy with explicit unresolved-link handling; do not simply abandon BLE tasks and start another device.

## SURVIVED

- **Prior findings:** overlapping BM2-window acceptance is fixed; the random-flood test is deterministic and checks the intended work bound; BM6’s categorical chance claim is corrected. The eager-subscription loss and repeated-warning concerns are also genuinely fixed. Only the frozen-prefix finding persists.

- **BM2 zero tail — (b), qualified assumption:** all **seven distinct inline captures** from KrystianD and doubleagent pass. However, doubleagent’s decompiled-app screenshot explicitly parses bytes **6–7 as `timeC`**, alongside bytes 4–5 as `timeB`. They are not established padding. Any nonzero `timeC` is rejected. No observed rollover or population rejection rate was established; seven passing examples cannot quantify fleet compatibility. [Primary article, relocated](https://haxrob.net/bm2/bm2-reversing-the-ble-protocol-of-the-bm2-battery-monitor/).

- **BM6 sign — (b), qualified assumption:** tarball, JeffWDH and Rafciq decode `01` as negative and everything else as positive. That supports the interpretation, not an exhaustive `00/01` invariant. No contradictory non-sentinel capture was found. Exact sentinel handling remains consistent with [PR #412](https://github.com/fl4p/batmon-ha/pull/412). JeffWDH and tarball are not independent evidence.

- **Notification boundaries:** the inspected ESPHome, aioesphomeapi and habluetooth paths preserve individual notification values. A 16-byte value fits the minimum ATT MTU; TCP coalescing does not merge application notifications. No transport-induced split/coalesced BM realtime frame was established. Other firmware formats remain a compatibility assumption—not grounds to restore arbitrary AES-window scanning.

- **Sliding stream buffer:** all existing stream validators scan/resynchronize; none requires the original buffer start. Late split replies for all five stream protocols passed after flooding. Prefix trimming preserves a contiguous suffix and introduces no new stale/new-byte splice. It can evict a reply before polling: a 300-byte JK reply followed by **3797 junk bytes** loses its first byte. Also, the universal “<1 ms” comment is inaccurate: a header-heavy Daly2 check measured about **19.7 ms**, though work is now bounded.

- **Budget and shared lifecycle:** the default sum is **797.5 seconds**, with no identified default arithmetic error under prompt cancellation. It does not impose that delay automatically: a silent BM-only device with prompt operations waits roughly 24 seconds. An eligible-probe budget would tighten the ceiling, but not repair cancellation semantics. Scanner retry disconnect/start lack individual bounds but sit inside detection’s 60-second wrapper. Ordinary driver callers lack that wrapper. Detection itself does not acquire `ConnectLock`.

- **Scanner-stop behavior:** normal completion survives. Stop exceptions now produce a warning rather than failing a successful connection—a real behavior change. That warning does not establish that scanning stopped. No additional proxy connection-slot leak from this restructuring was demonstrated.

- **Calibration gaps:** all 56 tests still pass after removing the **write timeout**, **discovery timeout**, **scanner budget term**, **scanner finally**, or **BM2 doubled wait**. The hanging-subscription test never reaches its hanging write. Removing the scanner term matters because unrelated slack hides the omission. Test each stalled operation independently, exercise the real scanner method, and check individual budget terms or a virtual-clock worst-case trace. The tail/sign checks, both caps, eager-notification preservation, and warning throttling all failed their corresponding mutation tests.
