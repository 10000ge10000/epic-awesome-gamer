#!/bin/sh
set -eu
umask 077

container="epic-warp"
health_url="http://127.0.0.1:${WARP_CONTROL_PORT:-18080}/health"
state_dir="${WARP_WATCHDOG_STATE_DIR:-/var/lib/epic-kiosk}"
fail_file="$state_dir/warp-watchdog-failures"

mkdir -p "$state_dir"
failures=0
[ -f "$fail_file" ] && failures=$(cat "$fail_file" 2>/dev/null || printf '0')

if docker exec "$container" curl -fsS --max-time 10 "$health_url" >/dev/null 2>&1; then
    printf '0' > "$fail_file"
    exit 0
fi

failures=$((failures + 1))
printf '%s' "$failures" > "$fail_file"
[ "$failures" -ge 3 ] || exit 0

docker inspect "$container" >/dev/null 2>&1
docker restart "$container" >/dev/null
printf '0' > "$fail_file"
