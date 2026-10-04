# `type: auto` — design plan (draft, under review)

Status: proposal, 2026-10-04. Not implemented.

## Motivation

Users pick the wrong `type:` and get silent timeouts. #416: a Daly module named
`DL-D6C14E1000D1` with GATT service `fff0` (`fff1` notify, `fff2`/`fff3` write)
was configured as `daly2` (Modbus, `D2 03 …` frames). It ignored every request; a
`type: snoop:daly` run showed it answers classic Daly `A5 …` 13-byte frames
(`type: daly`).

## Why advertisement matching alone is insufficient

- aiobmsble ships `aiobmsble.utils.bms_identify(adv_data, mac)` (local name,
  service UUID, manufacturer id, OUI). Its `daly_bms` matcher claims `DL-*`, but
  `daly_bms` speaks Modbus (`D2`). The #416 module is `DL-*` and speaks `A5`, so
  advert matching alone would pick the wrong driver.
- Through an ESPHome proxy, batmon's `bt_diagnostics` reported `name=None` for
  that device, so name-based matching can find nothing.

## Proposed design

1. **Shortlist from the advertisement.** Use the advert (aiobmsble matchers plus
   batmon-native knowledge) only to order candidate types, never to decide.
2. **Confirm by probing.** For each candidate: connect, send that driver's own
   read-only poll on that driver's own TX characteristic, accept the type only on
   a well-formed reply with a valid checksum/CRC. Families with two protocols
   (Daly `A5`/`D2` for now) get both tried.
3. **Persist the result** per MAC under `/data` (so it doesn't probe every
   start). Log `auto: <MAC> is type daly, put type: daly in your config`.
4. **Fail loudly.** If nothing answers, raise listing what was tried, suggest
   `type: snoop:<families>`. No fallback guess.
5. **BLE only.** Wired ports are out of scope (shared bus, baud sweep).

## Safety

Only read polls, only to each candidate's own vendor characteristic (what that
driver would send anyway). Never write SIG characteristics (0x2A00–0x2BFF) — see
commit 1df22ec: snoop's probe wrote GAP Device Name through an ESPHome proxy and
a Daly module stored the probe frame as its name (#416).

## Implementation sketch

- Resolver step before `construct_bms` (`bmslib/models/__init__.py:114`),
  which today maps `type:` → class via `get_bms_model_class` (batmon-native
  registry at `bmslib/models/__init__.py:36`, else aiobmsble `<name>_bms`
  wrapped by `BLE_BMS_wrap`).
- Table: candidate type → (rx uuid, tx uuid, poll frame(s), reply validator).
  `bmslib/models/snoop.py` `PROBE_FRAMES` + its response fingerprints cover much
  of it.
- Per-MAC cache file in `/data`.
- Tests with recorded replies.
- Start set: Daly (`daly`, `daly2`), JBD, JK, ANT, plus whatever the advert
  matches. Estimated ~1 day.

## Revision after the Codex plan review (2026-10-04)

Review log: `~/codex-reviews/batmon-type-auto-plan/review.log`. Implemented in
`bmslib/auto_detect.py`; how each finding was handled:

1. *Probe safety* — audited allowlist only (`_build_probes`): Daly A5 0x90/0x94,
   Daly2 read 0x0000×0x3E, JBD 0x03/0x04, JK 0x97 only (not 0x96, which starts
   the stream), ANT status. No driver `connect()`/`fetch()` is run, no aiobmsble
   driver is probed. Cross-vendor meaning of these frames stays a documented,
   unverified residual risk.
2. *Weak acceptance* — detection-specific validators: reply-side address
   (rejects echoes), command, exact length, checksum; buffer cleared per
   request, so a reply notified before the request doesn't count; two requests
   for 8-bit-checksum protocols (Daly, JBD). Tests for the two false-acceptance
   cases the review gave.
3. *Overlap* — validators are mutually exclusive by framing (tested); detection
   maps to the native types explicitly (`ADVERT_HINTS`: aiobmsble `daly_bms` →
   `daly2`, `daly`). JK maps to `jk`, which already picks its variant.
4. *Snoop* — not used as a table or as a fallback; the failure message points
   to a passive `type: snoop` log.
5. *Cache* — dropped: detection runs every start, nothing is persisted.
   Unreachable (`connected=False`) is reported as unverified, not as a verdict.
6. *Lifecycle* — async, before construction, one device at a time (proxy
   slots), `DETECT_TIMEOUT` 60 s per device, 3 connect attempts, unconditional
   disconnect, exceptions isolated per device; an unresolved device is skipped
   (address commented out), the others start.
7. *Advertisements* — `bt_discovery()` now records every advertisement in
   `bmslib.bt.discovered_adverts` (all adapters); all aiobmsble matchers are
   evaluated, not just the first; a missing advertisement only means no ordering.
8. *Scope* — the five native families; other aiobmsble types the advertisement
   matches are named in the failure message, never probed.
