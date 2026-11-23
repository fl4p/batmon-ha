# Documentation rules

`README.md` is user reference, not a lab notebook.

- An option or type gets what it does, its default, and when to change it, in one or two lines.
- Put details where they belong instead:
  - implementation (retry timings, register maps, frame bytes, why something is global): code comments
  - hardware and protocol findings, per-device reports: `doc/dev/BMS_FACTSHEET.md`
  - what changed and when: `CHANGELOG.md`
  - topic deep dives: their own page under `doc/`, linked from the README
- No issue narratives ("three packs were confirmed working once…"), no "now"/"no longer"/"was" wording, no
  remarks about how sure we are ("no claim that…"). A workaround goes to Troubleshooting as one bullet.
- Before adding to the README, check whether the fact is already in the CHANGELOG, the fact sheet or the code. If it
  is, link or leave it out.
- Fixing a bug rarely needs a README change. Add one only if user-visible behaviour or configuration changed.

# havan (shared host)

havan also runs the MQTT broker, the console logger and the telemetry relay that the fugu converters depend on.
Before rebooting it, or stopping its networking or services, take `~/bin/lock.sh acquire havan "<why>"` (check the
result directly) and announce on the `fugu` channel; release the lock once it is back
(`~/dev/kb/agent-ops/reboot-a-shared-host-only-under-its-lock.md`).
