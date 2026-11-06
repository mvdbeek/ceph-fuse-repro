#!/usr/bin/env bash
# Capture client, kernel-visible and cluster state into a directory.
#   harness/capture.sh <outdir> [reason]
# Runs on the host and reaches into the containers with `docker exec`.
set -uo pipefail
out=$1
reason=${2:-manual}
mkdir -p "$out"
echo "$reason" >"$out/reason"
date -u +%FT%TZ >"$out/time"

ex() { # container file cmd...  (stdout+stderr into file, never fails)
    local c=$1 f=$2; shift 2
    timeout 60 docker exec "cfr-$c" "$@" >"$out/$f" 2>&1 || echo "[exit $?]" >>"$out/$f"
}

for c in node head; do
    asok=/var/run/ceph/ceph-client.galaxy-$c.asok
    d=$c
    mkdir -p "$out/$d"
    # Kernel FUSE connection counters (needs fusectl; not mountable in rootless docker).
    ex "$c" "$d/fusectl.txt" bash -c 'ls -la /fusectl; for x in /fusectl/*/; do for k in waiting max_background congestion_threshold; do echo "$x$k=$(cat $x$k 2>&1)"; done; done'
    ex "$c" "$d/ps.txt" ps -eLo pid,tid,stat,wchan:40,etimes,time,comm,args
    # Every D-state task: wchan, kernel stack (needs init-ns CAP_SYS_ADMIN), syscall, status.
    ex "$c" "$d/dstate.txt" bash -c '
        for t in /proc/[0-9]*/task/[0-9]*; do
            s=$(cut -d")" -f2 $t/stat 2>/dev/null | cut -d" " -f2)
            [ "$s" = D ] || continue
            echo "=== $t comm=$(cat $t/comm) wchan=$(cat $t/wchan)"
            echo "cmdline: $(tr "\0" " " < ${t%/task/*}/cmdline)"
            echo "syscall: $(cat $t/syscall 2>&1)"
            echo "--- stack"; cat $t/stack 2>&1
            echo "--- status"; grep -E "State|ctxt" $t/status
        done'
    ex "$c" "$d/sysrq.txt" bash -c 'echo w > /proc/sysrq-trigger && echo "sysrq-w sent (output goes to the host kernel log)"'
    ex "$c" "$d/dmesg.txt" dmesg --ctime
    for cmd in status mds_sessions mds_requests objecter_requests dump_ops_in_flight "perf dump" "config diff" dump_cache; do
        ex "$c" "$d/asok-${cmd// /_}.json" ceph --admin-daemon "$asok" $cmd
    done
    ex "$c" "$d/mountinfo.txt" cat /proc/self/mountinfo
    # Write the in-memory log ring (debug_client gather level) to the log, then copy the tail.
    ex "$c" "$d/asok-log_dump.txt" ceph --admin-daemon "$asok" log dump
    sleep 1
    ex "$c" "$d/client.log.tail" tail -n 300000 "/var/log/ceph/ceph-client.galaxy-$c.log"
    gzip -f "$out/$d/client.log.tail" "$out/$d/asok-dump_cache.json" 2>/dev/null
done

mkdir -p "$out/cluster"
ex mon cluster/status.txt ceph -s
ex mon cluster/health.txt ceph health detail
ex mon cluster/blocklist.txt ceph osd blocklist ls
ex mon cluster/session_ls.json ceph tell mds.a session ls
ex mon cluster/mds_ops.json ceph tell mds.a dump_ops_in_flight
ex mon cluster/mds_blocked.json ceph tell mds.a dump_blocked_ops
ex mon cluster/mds_perf.json ceph tell mds.a perf dump
for i in 0 1 2; do
    ex osd$i cluster/osd$i-ops.json ceph --admin-daemon /var/run/ceph/ceph-osd.$i.asok dump_ops_in_flight
done
ex mon cluster/config_dump.txt ceph config dump
cp "$(dirname "$0")/../state/ctl/events.jsonl" "$out/" 2>/dev/null || true
echo "captured into $out"
