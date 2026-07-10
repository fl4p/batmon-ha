#!/bin/sh
# Container entry point. The same image layout serves two very different runtimes:
#
#  - Home Assistant add-on: base image ships s6-overlay + bashio. addon_main.sh's
#    shebang (#!/usr/bin/with-contenv bashio) does the right thing, so just exec
#    it, exactly as the old CMD ["./addon_main.sh"] did.
#
#  - Standalone Docker (BUILD_FROM=alpine, see doc/Docker.md): no supervisor, no
#    bashio, and no /usr/bin/with-contenv — the shebang is unusable, so we have
#    to name the interpreter ourselves. addon_main.sh detects the missing bashio
#    and reads options.json directly.
#
# /usr/bin/with-contenv is a structural invariant of the HA base images rather
# than a heuristic, which is why we test for it instead of for bashio.
if [ -x /usr/bin/with-contenv ]; then
  exec /app/addon_main.sh
fi

exec /bin/sh /app/addon_main.sh
