#!/usr/bin/with-contenv bashio

# Entry point for BOTH the Home Assistant add-on and the standalone Docker image.
#
# Under HA the shebang runs this with `with-contenv bashio`, so the bashio::*
# functions and SUPERVISOR_TOKEN are present. In a plain container (built with
# BUILD_FROM=alpine, see doc/Docker.md) entrypoint.sh invokes us as `/bin/sh
# addon_main.sh` instead: bashio does not exist, and the wrappers below fall
# back to reading options.json directly.
#
# Two constraints for that standalone path:
#  - POSIX sh only (busybox ash). Never *define* a function whose name contains
#    "::" — ash rejects that. *Calling* bashio::config is fine, it is just a word.
#  - Guard every bashio:: call with _has_bashio, otherwise it exits 127 and, with
#    no `set -e`, the script silently carries on with a wrong value.

# https://community.home-assistant.io/t/cannot-find-supervisor-token-environment-variable/543209/6
# https://github.com/hassio-addons/bashio/blob/main/lib/addons.sh

# set +e # continue script on error

# Fixed interpreter for reading options.json. Not $PYBIN: that is reassigned to
# the esphome venv later, and the stack it is reassigned *by* is the one we read
# here.
_CFG_PY=/app/venv/bin/python3

_has_bashio() { command -v bashio::config >/dev/null 2>&1; }

# Read a top-level string option from options.json, using the same lookup order
# as the python side (load_user_config in bmslib/store.py, _early_select_ble_stack
# in main.py). A missing file, a missing key, null, and non-string values all
# yield "" so callers can fall through to their default.
_cfg_json() {
  "$_CFG_PY" -c '
import json, sys
for path in ("/data/options.json", "options.json"):
    try:
        value = json.load(open(path)).get(sys.argv[1])
    except Exception:
        continue
    print(value if isinstance(value, str) else "")
    break
' "$1" 2>/dev/null
}

cfg()  { if _has_bashio; then bashio::config "$1"; else _cfg_json "$1"; fi; }
log()  { if _has_bashio; then bashio::log.blue "$@"; else echo "$@"; fi; }
warn() { if _has_bashio; then bashio::log.warning "$@"; else echo "$@" >&2; fi; }

if _has_bashio && bashio::config.exists "install_newer_bleak"; then
  bashio::addon.option "install_newer_bleak" # delete
fi

# query MQTT details from supervisor API
# see e.g. https://github.com/zigbee2mqtt/hassio-zigbee2mqtt/blob/master/common/rootfs/docker-entrypoint.sh
# also https://github.com/wmbusmeters/wmbusmeters-ha-addon/blob/main/wmbusmeters-ha-addon%2Frun.sh
#
# Leave MQTT_* unset when there is no supervisor: assigning "" here would clobber
# values the user passed with `docker run -e MQTT_HOST=...` (see the exec below).
if _has_bashio && bashio::services.available 'mqtt'; then
  MQTT_HOST="$(bashio::services 'mqtt' 'host')"
  MQTT_PORT="$(bashio::services 'mqtt' 'port')"
  MQTT_USER="$(bashio::services 'mqtt' 'username')"
  MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
  log "MQTT broker:     $MQTT_USER@$MQTT_HOST:$MQTT_PORT"
else
  log "MQTT service not configured in HA. Using broker credentials from add-on configuration."
fi

# Select the BLE stack. "bumble" routes `import bleak` (incl. inside aiobmsble)
# to a drop-in by prepending that package's bundled shadow dir to PYTHONPATH:
#   "bumble" -> bumble-bleak (pure-Python HCI, no BlueZ/D-Bus; takes the adapter
#               via an HCI User Channel; SMP pairing done inline).
#   "bluek"  -> bluek (talks to the kernel BlueZ stack over L2CAP/mgmt sockets,
#               no D-Bus; coexists with bluetoothd; pairing via bluetoothctl).
# Both skip the forked-bleak BlueZ pair-only pre-step. Default "bleak" keeps the
# stock BlueZ/D-Bus stack (and the forked-bleak pairing step).
BLE_STACK="$(cfg 'ble_stack')"
SHADOW_PYTHONPATH=""
PYBIN="/app/venv/bin/python3"

# config.yaml defaults this to "bleak", but a hand-written options.json may omit
# it entirely (and bashio yields "null" for an unset key). Normalize, so the
# pair-only pre-step below is not skipped just because the key is absent.
case "$BLE_STACK" in
  ""|null) BLE_STACK="bleak" ;;
esac

# Map the selected stack to its shadow python package (bumble/bluek) or to a
# dedicated venv (esphome — needs bleak>=3 which conflicts with the bleak==2
# pin in `venv`).
SHADOW_PKG=""
case "$BLE_STACK" in
  bumble)  SHADOW_PKG="bumble_bleak"; STACK_LABEL="bumble-bleak (no BlueZ/D-Bus, exclusive HCI)" ;;
  bluek)   SHADOW_PKG="bluek";        STACK_LABEL="bluek (kernel BlueZ sockets, no D-Bus, coexists)" ;;
  esphome) STACK_LABEL="esphome (Bluetooth Proxy via aioesphomeapi/habluetooth, no local adapter)"
           if [ -x /app/venv_esphome/bin/python3 ] \
              && /app/venv_esphome/bin/python3 -c "import habluetooth, bleak_esphome, aioesphomeapi" 2>/dev/null; then
             PYBIN="/app/venv_esphome/bin/python3"
             log "BLE stack: $STACK_LABEL (venv_esphome)"
           else
             warn "ble_stack=esphome but venv_esphome is missing deps; falling back to bleak"
             BLE_STACK="bleak"
           fi ;;
esac

if [ -n "$SHADOW_PKG" ]; then
  SHADOW_DIR="$(/app/venv/bin/python3 -c "import ${SHADOW_PKG} as m, os; print(os.path.join(os.path.dirname(m.__file__), '_shadow'))" 2>/dev/null)"
  if [ -n "$SHADOW_DIR" ] && [ -d "$SHADOW_DIR" ]; then
    SHADOW_PYTHONPATH="$SHADOW_DIR"
    log "BLE stack: $STACK_LABEL, shadow=$SHADOW_DIR"
  else
    warn "ble_stack=$BLE_STACK but $SHADOW_PKG is not installed; falling back to bleak"
    BLE_STACK="bleak"
  fi
fi

if [ "$BLE_STACK" = "bleak" ]; then
  log "BLE stack: bleak (BlueZ/D-Bus)"
  # Check the status explicitly. bashio runs us with errexit, so on the add-on
  # path a failure here already aborts; plain sh has no such thing, and silently
  # continuing would launch main.py against an unpaired device, surfacing as
  # confusing BLE connect errors instead of a pairing failure.
  if ! /app/venv_bleak_pairing/bin/python3 main.py pair-only; then
    warn "BLE pairing pre-step failed"
    exit 1
  fi
fi

# Export only what we actually have. main.py ignores empty MQTT_* anyway, but
# assigning them here would overwrite a standalone user's `-e MQTT_HOST=...`.
#
# bashio runs us with `set -o nounset -o errexit`, so expand with ${x:-} (the
# vars are genuinely unset when there is no supervisor) and use if-blocks: a
# trailing `[ -n "$x" ] && export x` would leave the script's exit status at 1.
if [ -n "${MQTT_HOST:-}" ];     then export MQTT_HOST;     fi
if [ -n "${MQTT_PORT:-}" ];     then export MQTT_PORT;     fi
if [ -n "${MQTT_USER:-}" ];     then export MQTT_USER;     fi
if [ -n "${MQTT_PASSWORD:-}" ]; then export MQTT_PASSWORD; fi
if [ -n "$SHADOW_PYTHONPATH" ]; then export PYTHONPATH="$SHADOW_PYTHONPATH"; fi

# exec: let SIGTERM from `docker stop` reach python instead of the shell.
exec "$PYBIN" main.py
