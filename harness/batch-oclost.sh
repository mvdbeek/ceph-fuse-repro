#!/usr/bin/env bash
# Run plan for harness/oc_lostread.sh: repeats, controls and the escalation to
# max_background lost reads. Waits for HEALTH_OK between runs.
cd "$(dirname "$0")/.."
waitok() { for i in $(seq 60); do docker exec cfr-mon ceph health | grep -q HEALTH_OK && return; sleep 5; done; }
for args in "punch churn 1" "truncate churn 1" "punch churn 1" "none churn 1" \
            "truncate churn 1" "punch churn 1" "punch churn 12"; do
    waitok
    echo "#### $args"
    timeout 900 harness/oc_lostread.sh $args | grep -E "^== (READ|all|discards|zero|elapsed|node)|HUNG|ok \(rc"
done
