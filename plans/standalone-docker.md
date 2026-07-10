# Ship a standalone Docker image from upstream batmon-ha

## Context

Two external efforts want batmon-ha in a plain container:

- **PR [#120](https://github.com/fl4p/batmon-ha/pull/120)** (bentolor, open) adds `doc/Docker.md` + `doc/options.json.template`. It has rotted: it patches `run.sh`, which we renamed to `addon_main.sh`, and its `ARG BUILD_FROM=...` default is already in our Dockerfile.
- **[sashasimkin/batmon-docker](https://github.com/sashasimkin/batmon-docker)** ships no Dockerfile at all. Its CI checks out *our* repo, runs `sed -i '1s|.*|#!/bin/sh|' addon_main.sh`, and builds with `BUILD_FROM=alpine`. It also has a Helm chart. **No LICENSE file — none of it can be copied.**

That sed only works by accident. `addon_main.sh` has shebang `#!/usr/bin/with-contenv bashio`; under `/bin/sh` every `bashio::` call exits 127 and the script continues (no `set -e`). `BLE_STACK` ends up empty, the `case` matches nothing, `[ "" = "bleak" ]` is false. Net result: **`ble_stack` is silently ignored** (bumble/bluek/esphome unreachable), the forked-bleak `pair-only` pre-step is skipped, and MQTT env vars are empty.

Shipping this properly is now more attractive than when fl4p wrote *"I don't recommend running batmon in Docker anyway"* on #120. That was 2023, when BLE meant BlueZ + D-Bus. Since then `ble_stack: bluek` needs no D-Bus, and `ble_stack: esphome` needs no local adapter at all — it containerizes cleanly with zero privileges. The multi-stack work is exactly what the sed hack throws away.

**Outcome:** one Dockerfile produces both the HA add-on image and a standalone image where `ble_stack` actually works; upstream publishes `ghcr.io/fl4p/batmon-ha`; Docker, Compose, and Helm users have supported paths.

## Bug found while planning

`addon_main.sh:23-26` sets `MQTT_HOST=""` (etc.) when the Supervisor MQTT service is absent, and line 76 exports those empty strings into `main.py`'s environment. That **clobbers any `docker run -e MQTT_HOST=...`** the user passed. `main.py:272` only consumes non-empty env vars, so the value is silently dropped. This is why standalone MQTT config only ever worked via `options.json`. Fix it as part of this work.

## Design decisions

- **One entrypoint script, bashio-optional.** Keep a single `addon_main.sh` and route its bashio calls through thin `cfg`/`log`/`warn` wrappers that delegate to bashio when present. Rejected the alternative (freeze `addon_main.sh`, add a parallel `standalone_main.sh`) because it duplicates the `ble_stack` `case` block, which changed in 3 of the last 5 releases and would drift.
- **No `bash` dependency.** `alpine:latest` has no bash, and busybox `ash` rejects *defining* functions whose names contain `::`. We never define one — we only *call* `bashio::config`, which ash parses fine as a command word. So no `apk add bash`.
- **`apparmor.txt` needs no change.** Line 9's bare `file,` rule already permits exec of anything under `/app`, which is how the current `CMD ["./addon_main.sh"]` runs.
- **`config.yaml` needs no change** beyond the version bump. It is HA-only metadata.

## Changes

### 1. `entrypoint.sh` (new, `#!/bin/sh`) — the new `CMD`

Discriminates on `/usr/bin/with-contenv`, a structural invariant of the HA base images and absent from plain Alpine:

```sh
#!/bin/sh
# HA add-on: let addon_main.sh's shebang run it under `with-contenv bashio`,
# exactly as the previous CMD ["./addon_main.sh"] did.
[ -x /usr/bin/with-contenv ] && exec /app/addon_main.sh
# Standalone: no Supervisor, no bashio. Invoke the interpreter explicitly so the
# unusable shebang is bypassed.
exec /bin/sh /app/addon_main.sh
```

### 2. `addon_main.sh` — make the bashio calls optional

Add near the top, then replace every direct `bashio::` call with a wrapper. `_CFG_PY` is fixed at `/app/venv/bin/python3` (not `$PYBIN`, which gets reassigned for the esphome stack after `cfg` is called):

```sh
_CFG_PY=/app/venv/bin/python3
_has_bashio() { command -v bashio::config >/dev/null 2>&1; }
log()  { if _has_bashio; then bashio::log.blue    "$@"; else echo "$@"; fi; }
warn() { if _has_bashio; then bashio::log.warning "$@"; else echo "$@" >&2; fi; }
cfg()  { if _has_bashio; then bashio::config "$1"; else _cfg_json "$1"; fi; }
```

`_cfg_json` resolves `options.json` with the **same precedence the Python side uses** — `/data/options.json` then `options.json` in cwd (`bmslib/store.py:62-71`, `main.py:24`) — and collapses missing file, missing key, `null`, and non-string values to `""`, so they fall through the `case` to the `bleak` default.

Per-call mapping:

| current | becomes |
|---|---|
| `bashio::config.exists "install_newer_bleak"` + `bashio::addon.option` (L8-10) | wrap in `if _has_bashio` — it only mutates a Supervisor-managed option, meaningless standalone |
| `bashio::services.available 'mqtt'` (L16) | `if _has_bashio && bashio::services.available 'mqtt'` |
| `bashio::services 'mqtt' …` (L17-20) | unchanged, inside that guarded branch |
| `bashio::config 'ble_stack'` (L38) | `cfg ble_stack` |
| `bashio::log.*` | `log` / `warn` |

**Fix the MQTT clobber (L76):** stop unconditionally assigning possibly-empty MQTT vars on the exec line. Export each only when non-empty, so a user's `-e MQTT_HOST=...` survives into `main.py`. Behavior on the HA path is unchanged, because `main.py:272` already ignores empty env vars. Same for `PYTHONPATH`. Also add `exec` to the final `main.py` invocation so `docker stop`'s SIGTERM reaches Python instead of dying at the shell.

Everything else in the script — the stack `case`, the `_shadow` dir probe, the `venv_esphome` import check, the `pair-only` pre-step — stays as written and now runs in both worlds.

### 3. `Dockerfile`

- `RUN chmod a+x addon_main.sh entrypoint.sh`
- `CMD ["./addon_main.sh" ]` → `CMD ["/app/entrypoint.sh"]`

Nothing else. Standalone builds pass `--build-arg BUILD_FROM=alpine:3.21`; the existing `apk add python3~3.13 || … || apk add python3` fallback already tolerates plain Alpine.

### 4. `.dockerignore` (new)

There is none today, so a *local* `docker build` bakes the untracked dev `options.json` — which holds real-looking MQTT and InfluxDB credentials — plus `bms_meter_states.json`, `user_id`, and any local `venv/` into the image. CI builds from a clean checkout are unaffected (those paths are gitignored), but this is a live credential-leak footgun for anyone who follows the new Docker docs on their own machine. List: `options.json`, `bms_meter_states.json`, `bat_*.json`, `user_id`, `.env`, `venv*`, `__pycache__`, `.git`, `.idea`.

Excluding `options.json` also makes the standalone contract deterministic: config always resolves to the mounted `/data/options.json`.

### 5. `.github/workflows/docker.yml` (new)

Repo has no workflows yet (only `.github/ISSUE_TEMPLATE/`).

- **Tags** derived from `config.yaml`'s `version:` field — the single source of truth we already bump per release — plus `latest` and `type=sha`. Upstream doesn't cut git tags, so `type=semver` would fire on nothing.
- **Triggers:** push to `master` (path-filtered), `pull_request` (build only, no login/push), `workflow_dispatch`.
- **Registry** `ghcr.io/${{ github.repository }}`, auth via `GITHUB_TOKEN` with `packages: write`. No secrets to provision.
- **Platforms** `linux/amd64,linux/arm64,linux/arm/v7`. Use the per-platform **matrix + digest merge** pattern (build each platform in its own job, export digests, join with `buildx imagetools create`) rather than one job with three platforms — the three run in parallel instead of serially, which matters because arm64/armv7 are QEMU-emulated and this Dockerfile builds four venvs with several `git+https` installs.
  - Document in the workflow that **armv7 ships without the esphome stack**: no musl armv7 wheels exist for `cryptography`/`dbus-fast`/`bleak-esphome`, and `Dockerfile:63-70` deliberately declines to pull in a ~400MB Rust toolchain. `venv_esphome` fails, `|| true` swallows it, and the entrypoint falls back to bleak.
- **Caching** `type=gha`, scoped per platform. Note the trap: the unpinned `git+https` installs (`aiobmsble`, `bumble-bleak`) key their layer on the `RUN` string, not on upstream HEAD, so a warm cache serves a stale revision until something busts it.
- **Base pinned to `alpine:3.21`**, not `alpine:latest` — otherwise the `python3~3.13 || python3~3.12` fallback silently changes Python minor version, and musl wheel availability shifts under us.
- **`validate-addon` job:** native amd64, `push: false`, `BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest`. Cheap guard so a Dockerfile change can't break the HA add-on unnoticed.

### 6. Helm chart (new, `charts/batmon-ha/`)

Written from scratch — sashasimkin's is unlicensed. Chart.yaml, values.yaml, templates for Deployment + ConfigMap + optional PVC.

- `replicas: 1`, `strategy: Recreate` — a BLE adapter can't be shared.
- **`/data` must be a writable directory, not a mounted file.** `bmslib/store.py:18` sets `root_dir='/data/'` only when `/data/options.json` is readable, and then writes `bms_meter_states.json`, `bat_state_*.json`, and `user_id` there. Mount a PVC (or emptyDir) at `/data`, and nest the ConfigMap's `options.json` on top via `subPath` at `/data/options.json`. Kubelet orders nested mounts by path length, so this composes. (Fallback if it misbehaves on some kubelet: an initContainer that copies the ConfigMap into the PVC.) Downstream mounts only the file, so their meter state is silently lost on every pod restart — call that out in a comment.
- **Default `ble_stack: esphome`** in `values.yaml`: no privileges, no hostNetwork, no node affinity. It is the only stack that makes unqualified sense in a cluster. `bluek`/`bumble` require `hostNetwork: true` + `NET_ADMIN` + `NET_RAW` + a `nodeSelector` pinning to the node with the adapter — support them via values, but don't default to them.
- Checksum annotation on the config so a values change rolls the pod.

### 7. Docs

- **`doc/Docker.md` (new).** Lead with a table mapping each `ble_stack` to the exact flags it needs:

  | `ble_stack` | network | caps | D-Bus | adapter |
  |---|---|---|---|---|
  | `esphome` | bridge | none | none | none — via ESPHome BT proxy |
  | `bleak` (default) | bridge | none | `-v /run/dbus:/run/dbus:ro` | host `bluetoothd` |
  | `bluek` | `--net=host` | `NET_ADMIN`, `NET_RAW` | optional (pairing only) | shared, coexists |
  | `bumble` | `--net=host` | `NET_ADMIN`, `NET_RAW` | none | **exclusive** — brings it down |

  Plus: `--device /dev/ttyUSB0` for wired BMSes (`jk_uart`/`daly_uart`) — a bare `-v` of the node is not enough, the cgroup rule is what `--device` grants. And `--net=host` doesn't exist on Docker Desktop for macOS/Windows, so those users need `esphome`. `docker run` + Compose examples for the esphome and bluek cases. Mount `-v ./batmon-data:/data`, a directory, for the reason in §6. Link the Helm chart and credit `sashasimkin/batmon-docker` as prior art.
- **`doc/options.json.template` (new),** per PR #120. Make it the one canonical example. `doc/Standalone.md` currently carries two *divergent* inline examples (lines 22-58 and the "Minimal" one at 108-117); point both at the template.
- **`doc/Standalone.md`:** replace the stale `# Docker` section (lines 103-104, pointing at issue #25 from 2023) with a link to `doc/Docker.md`.
- **`CHANGELOG.md` + `config.yaml`:** bump to `2.04`. Note the CHANGELOG is already dirty in the working tree from the JBD work — coordinate the entry.

## Verification

Static:
```
sh -n entrypoint.sh addon_main.sh && bash -n addon_main.sh   # parses under both shells
shellcheck -s sh entrypoint.sh
python3 -m pytest bmslib/test -q
helm lint charts/batmon-ha && helm template charts/batmon-ha
```

Standalone image — this is the behavior the sed hack gets wrong, so test it directly:
```
docker build --build-arg BUILD_FROM=alpine:3.21 -t batmon:standalone .
mkdir -p /tmp/bm && cp doc/options.json.template /tmp/bm/options.json
```
1. Set `"ble_stack": "bluek"` in `/tmp/bm/options.json`, run with `-v /tmp/bm:/data`, and confirm the log says `BLE stack: bluek, shadow=…`. Under the current sed hack this line never appears — that is the regression we are fixing.
2. Set `"ble_stack": "esphome"` and confirm it selects `venv_esphome`.
3. Omit `mqtt_broker` from `options.json`, pass `-e MQTT_HOST=test.mosquitto.org`, confirm `main.py` logs `connecting mqtt …@test.mosquitto.org` — proves the clobber is fixed.
4. Confirm `bms_meter_states.json` and `user_id` appear in `/tmp/bm/` after a run, proving `/data` resolved as a writable root.
5. `docker stop` returns promptly (SIGTERM reached Python via `exec`).
6. `docker history batmon:standalone | grep -c options.json` → 0, and no credentials in the image.

HA add-on path must not regress:
```
docker build --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest -t batmon:addon .
./test_and_deploy.sh          # rsync + rebuild + verify on havan.local, waits for a BmsSampl( log line
```
Confirm the add-on log still prints the bashio-colored `MQTT broker: …@…` line, proving `bashio::services` still resolves through the wrappers.

Then push a branchless commit to `master` (per repo convention), let the workflow build, and `docker pull ghcr.io/fl4p/batmon-ha:2.04` on an arm64 host.

## Files

New: `entrypoint.sh`, `.dockerignore`, `.github/workflows/docker.yml`, `doc/Docker.md`, `doc/options.json.template`, `charts/batmon-ha/{Chart.yaml,values.yaml,templates/*}`

Modified: `addon_main.sh` (bashio wrappers + MQTT clobber fix + `exec`), `Dockerfile` (chmod + `CMD`), `doc/Standalone.md`, `config.yaml` (version), `CHANGELOG.md`

Unchanged on purpose: `apparmor.txt`, `build.yaml`
