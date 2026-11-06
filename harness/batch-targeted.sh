#!/usr/bin/env bash
# Targeted runs after the source review (results.md): transient cluster FULL,
# the ObjectCacher discard/trim race, a fault-free baseline, and a repeat.
#   harness/batch-targeted.sh [B|A]   > state/batch-targeted.log 2>&1
cd "$(dirname "$0")/.."
s=${1:-B}
# Wait for any run already in progress.
while pgrep -f '[h]arness/run.py' >/dev/null; do sleep 30; done
healthy() {
    for _ in $(seq 120); do
        docker exec cfr-mon ceph health 2>/dev/null | grep -q HEALTH_OK && return 0
        sleep 10
    done
    echo "cluster not HEALTH_OK after 20 min; stopping batch"; exit 1
}
run() { healthy; echo "=== $(date +%F\ %T) start $*"; python3 harness/run.py "$@"; echo "=== $(date +%T) end rc=$?"; }
hot="--workers 4 --hot 8 --hot-readers 8 --hot-interval 0.05 --hot-truncate"
run --stage "$s" --duration 3600 --faults fullflap --flap-seconds 6 --flap-interval 600 --label "$s-fullflap" -- --workers 4 --hot 4
run --stage "$s" --duration 3600 --faults octrim,slowosd,msinject --ms-socket-failures 500 --osd-delay-p 0.05 --osd-delay 0.3 \
    --label "$s-octrim-truncate-msinject" -- $hot
run --stage "$s" --duration 3600 --label "$s-baseline-2" -- --workers 4 --hot 4
run --stage "$s" --duration 3600 --faults fullflap --flap-seconds 6 --flap-interval 600 --label "$s-fullflap-2" -- --workers 4 --hot 4
echo "=== batch done $(date +%F\ %T)"
