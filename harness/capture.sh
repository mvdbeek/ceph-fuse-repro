#!/usr/bin/env bash
# Capture client, kernel-visible and cluster state into a directory.
#   harness/capture.sh <outdir> [reason]
# Runs on the host and reaches into the containers with `docker exec`, and into
# the Stage B guest (the "node" client when CFR_STAGE=B) over ssh.
set -uo pipefail
out=$1
reason=${2:-manual}
stage=${CFR_STAGE:-A}
mkdir -p "$out"
echo "$reason" >"$out/reason"
date -u +%FT%TZ >"$out/time"
echo "$stage" >"$out/stage"

run_in() { # name cmd...
    local c=$1; shift
    if [[ $c == node && $stage == B ]]; then
        docker exec cfr-vm ssh -i /vm/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR -o ConnectTimeout=10 -p 2222 root@127.0.0.1 "$(printf '%q ' "$@")"
    else
        docker exec "cfr-$c" "$@"
    fi
}

ex() { # name file cmd...  (stdout+stderr into file, never fails)
    local c=$1 f=$2; shift 2
    timeout 60 bash -c "$(declare -f run_in); stage=$stage; run_in $(printf '%q ' "$c" "$@")" >"$out/$f" 2>&1 \
        || echo "[exit $?]" >>"$out/$f"
}

for c in node head; do
    asok=/var/run/ceph/ceph-client.galaxy-$c.asok
    d=$c
    mkdir -p "$out/$d"
    # Kernel FUSE connection counters. Stage A: the host's fusectl (rootless
    # docker can't mount fusectl in a container, but the files belong to the
    # mounting uid, i.e. us). Stage B: the guest's fusectl.
    dev=$(timeout 20 bash -c "$(declare -f run_in); stage=$stage; run_in $c stat -c %d /mnt/scratch" 2>/dev/null)
    if [[ $c == node && $stage == B ]]; then
        ex "$c" "$d/fusectl.txt" sh -c "cd /sys/fs/fuse/connections && for x in *; do for k in waiting max_background congestion_threshold; do echo \"\$x/\$k=\$(cat \$x/\$k)\"; done; done; echo mount_dev=$dev"
    else
        { for k in waiting max_background congestion_threshold; do
              echo "$dev/$k=$(cat /sys/fs/fuse/connections/$dev/$k 2>&1)"; done; } >"$out/$d/fusectl.txt"
    fi
    ex "$c" "$d/ps.txt" ps -eLo pid,tid,stat,wchan:40,etimes,time,comm,args
    # Every D-state task: wchan, syscall, kernel stack (Stage B: root in the
    # guest; Stage A: needs init-namespace CAP_SYS_ADMIN, so it fails), status.
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
    # sysrq-w dumps blocked tasks into the kernel log (guest only), then read the log.
    ex "$c" "$d/sysrq.txt" bash -c 'echo w > /proc/sysrq-trigger && echo "sysrq-w sent" && sleep 1'
    ex "$c" "$d/dmesg.txt" dmesg --ctime
    for cmd in status mds_sessions mds_requests objecter_requests "perf dump" "config diff" dump_cache; do
        ex "$c" "$d/asok-${cmd// /_}.json" ceph --admin-daemon "$asok" $cmd
    done
    ex "$c" "$d/mountinfo.txt" cat /proc/self/mountinfo
    # No "log dump": in 17.2.9 it segfaulted ceph-fuse (Log::dump_recent ->
    # pthread_getname_np on an exited libfuse worker thread), and the crash then
    # hung in vfs_coredump behind the blocked invalidator. Use debugclient (log to
    # file) instead when the debug history is needed.
    ex "$c" "$d/client.log.tail" sh -c "tail -n 300000 \$(ceph-conf -n client.galaxy-$c --show-config-value log_file)"
    gzip -f "$out/$d/client.log.tail" "$out/$d/asok-dump_cache.json" 2>/dev/null
done
# Stage A: the host kernel log (readable with kernel.dmesg_restrict=0).
[[ $stage == A ]] && { dmesg --ctime 2>&1 | tail -n 2000 >"$out/host-dmesg.txt"; }

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
