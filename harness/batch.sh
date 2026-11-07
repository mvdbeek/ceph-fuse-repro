#!/usr/bin/env bash
# The long runs behind results.md, back to back: Stage B (production kernel in
# the KVM guest) first, then Stage A (host kernel), unless the argument says otherwise. Needs `make up vm-up`.
#   harness/batch.sh [B|A|AB]   > state/batch.log 2>&1
cd "$(dirname "$0")/.."
stages=${1:-BA}
# Wait for any run or batch already in progress.
while pgrep -f '[h]arness/run.py|[b]atch-targeted.sh' >/dev/null; do sleep 30; done
healthy() {
    for _ in $(seq 120); do
        docker exec cfr-mon ceph health 2>/dev/null | grep -q HEALTH_OK && return 0
        sleep 10
    done
    echo "cluster not HEALTH_OK after 20 min; stopping batch"; exit 1
}
run() { healthy; echo "=== $(date +%F\ %T) start $*"; python3 harness/run.py "$@"; echo "=== $(date +%T) end rc=$?"; }
for s in $(echo "$stages" | grep -o .); do
    run --stage "$s" --duration 3600 --label "$s-baseline" -- --workers 4 --hot 4
    run --stage "$s" --duration 7200 --faults netem,msinject,mdsrecall,slowosd --label "$s-all-faults" -- --workers 4 --hot 4
    run --stage "$s" --duration 7200 --faults mdsrecall,msinject --label "$s-recall-msinject" -- --workers 4 --hot 4
    run --stage "$s" --duration 5400 --faults mdsrecall --label "$s-recall-hot8" -- --workers 4 --hot 8 --hot-readers 8 --hot-interval 0.05
done
echo "=== batch done $(date +%F\ %T)"
