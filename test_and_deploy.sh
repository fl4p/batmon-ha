#!/usr/bin/env bash
# Run the test suite, deploy this tree to HA Supervised over SSH, rebuild
# the addon, and tail the container logs until a successful BMS sample is
# observed (or timeout).
#
# Reads HOST (and optional SLUG, SAMPLE_WAIT, SSH_OPTS) from ./.env.
# Exit codes: 0 verified, 1 verify failed/timeout, 2 tests failed,
#             3 missing/incomplete .env, other = step error (set -e).

set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="./.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# test_and_deploy.sh config
HOST=havan.local
# SLUG=batmon          # addon slug (default: batmon, deployed under .../local/batmon-ha)
# SAMPLE_WAIT=180      # seconds to wait for a successful BMS sample after start
# SSH_OPTS="-J jumphost -o HostKeyAlias=havan.local"   # extra ssh options (e.g. off-site)
EOF
  echo "Created template $ENV_FILE — edit HOST and re-run." >&2
  exit 3
fi
# shellcheck disable=SC1091
set -a; source "$ENV_FILE"; set +a
: "${HOST:?HOST not set in $ENV_FILE}"
SLUG="${SLUG:-batmon}"
SAMPLE_WAIT="${SAMPLE_WAIT:-180}"
# Supervisor renamed add-ons to apps: addons/local -> apps/local, addon_local_* -> app_local_*
REMOTE_DIR="/var/lib/homeassistant/apps/local/batmon-ha"
CONTAINER="app_local_${SLUG}"
SSH_OPTS="${SSH_OPTS:-}"
# SSH_OPTS is split into words on purpose
# shellcheck disable=SC2086
rsh() { ssh $SSH_OPTS "$@"; }

step() { printf '\n==> %s\n' "$*"; }

step "1/4 pytest (bmslib/test)"
# Test venv mirroring the image's main venv: requirements.txt pins plus
# aiobmsble --no-deps (it would drag bleak to 3.x, #275), at the version the
# Dockerfile pins. Rebuilt when requirements.txt or the Dockerfile change.
# PYTHON overrides the interpreter used to create it (default python3.13).
VENV=./venv
if [[ ! -f "$VENV/.stamp" || requirements.txt -nt "$VENV/.stamp" || Dockerfile -nt "$VENV/.stamp" ]]; then
  AIOBMSBLE=$(sed -nE "s/.*venv\/bin\/pip3 install --no-deps '(aiobmsble==[^']+)'.*/\1/p" Dockerfile | head -1)
  : "${AIOBMSBLE:?aiobmsble pin not found in Dockerfile}"
  rm -rf "$VENV"
  "${PYTHON:-python3.13}" -m venv "$VENV"
  "$VENV/bin/pip" install -q -r requirements.txt pytest numpy influxdb requests
  "$VENV/bin/pip" install -q --no-deps "$AIOBMSBLE"
  touch "$VENV/.stamp"
fi
if ! "$VENV/bin/python" -m pytest bmslib/test -q; then
  echo "Tests failed — aborting deploy." >&2
  exit 2
fi

step "2/4 rsync -> ${HOST}:${REMOTE_DIR}"
# --filter=':- .gitignore' tells rsync to read .gitignore and skip anything it
# excludes (so we don't have to duplicate that list here). The hand-written
# --excludes below cover things gitignore deliberately doesn't (e.g. .git
# itself, IDE/dev-host-only files, and options.json/user_id which the addon
# generates on the target).
rsync -a --delete -e "ssh ${SSH_OPTS}" \
  --filter=':- .gitignore' \
  --exclude=.git --exclude=doc --exclude=.idea \
  --exclude=options.json --exclude=.env \
  --exclude=user_id --exclude=.github \
  --exclude=test_and_deploy.sh \
  ./ "${HOST}:${REMOTE_DIR}/"

step "3/4 rebuild & restart local_${SLUG} (slow on Pi: pip installs run inside the image)"
# `ha apps rebuild` refuses with "Version changed, use Update instead" when
# config.yaml's version differs from the installed one, so try update first
# and fall back to rebuild for same-version source changes.
rsh "$HOST" "sudo ha apps stop local_${SLUG} || true; { sudo ha apps update local_${SLUG} || sudo ha apps rebuild local_${SLUG}; } && sudo ha apps start local_${SLUG}"

step "4/4 verify — tailing ${CONTAINER} up to ${SAMPLE_WAIT}s for a successful BMS sample"
# Success marker: `<name>: BmsSampl(...)` — emitted in bmslib/sampling.py whenever
# a parsed BmsSample is logged. That proves we connected AND decoded a frame.
# We use awk (not `tee | grep`): tee full-buffers its stdout when piped, so the
# match line can sit in a 4KB block buffer and never reach grep before the
# remote `timeout` kills the stream. awk with fflush echoes each line live AND
# exits 0 on first match (which SIGPIPEs ssh → ends the remote command cleanly).
set +e
rsh "$HOST" "sudo timeout ${SAMPLE_WAIT} docker logs -f --tail 0 ${CONTAINER} 2>&1" \
  | awk '
      { print; fflush() }
      /: BmsSampl\(/ { found=1; exit }
      END { exit found ? 0 : 1 }
    '
# Use awk's exit status, not $? — pipefail is on (set -euo pipefail above) and
# isn't cleared by `set +e`, so when awk matches and exits 0, ssh dies with
# SIGPIPE (141) and the pipeline status reflects ssh, not awk. PIPESTATUS[1]
# isolates awk's result.
RC=${PIPESTATUS[1]}
set -e

if [[ $RC -eq 0 ]]; then
  printf '\n==> verified: BMS connected and sampled on %s\n' "$HOST"
  exit 0
fi
printf '\n==> FAILED: no successful BMS sample within %ss. Check the log above.\n' "$SAMPLE_WAIT" >&2
exit 1
