#!/usr/bin/env bash
# Deterministic test of the ObjectCacher lost-read candidate (results.md,
# finding 7), Stage A, node client:
#
#  1. hold all replies from one OSD to the node client;
#  2. start buffered reads of N files on that OSD, so their buffers sit in rx;
#  3. discard those ranges from the same client while the reads are in flight:
#       punch    - fallocate punch hole (Client::_fallocate discards directly,
#                  no MDS cap revoke needed)
#       truncate - truncate -s 0 (goes through the MDS)
#       none     - no discard (control)
#     discard() turns the rx buffers into zero buffers that keep waitfor_read
#     but are no longer pinned in the LRU;
#  4. churn: read other files (on other OSDs) through a 2 MB client_oc_size so
#     trim() expires those zero buffers, waiters included (unless nochurn);
#  5. release the replies. bh_read_finish() finds no buffer, so nobody wakes
#     the readers, and each lost read keeps a FUSE request (and, for
#     readahead, a background slot) forever.
#
#   harness/oc_lostread.sh punch|truncate|none [churn|wchurn|nochurn] [N] [outdir]
#
# A hung run leaves the node client wedged; the script then probes the FUSE
# signature, captures, evicts the node's session and remounts it.
# CFR_STAGE=B runs the node side in the KVM guest (scripts/vm.sh) and also
# records kernel stacks and the hung-task report.
set -uo pipefail
mode=${1:?punch|truncate|none}
churn=${2:-churn}
nfiles=${3:-1}
out=${4:-results/oclost-${CFR_STAGE:-A}-$mode-$churn-n$nfiles-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$out"
exec > >(tee "$out/log.txt") 2>&1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAGE=${CFR_STAGE:-A}
D=/mnt/scratch/oclost
if [ "$STAGE" = B ]; then
    CLOG=$ROOT/state/log-vm/ceph-client.galaxy-node.log
    NODEIP_HEX=0x0ab14d28   # 10.177.77.40 (cfr-vm, the guest's slirp NAT)
    n() { docker exec -i cfr-vm ssh -i /vm/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR -p 2222 root@127.0.0.1 "$(printf '%q ' "$@")"; }
    fusectl() { n sh -c "cat /sys/fs/fuse/connections/\$(stat -c %d /mnt/scratch)/$1"; }
    abort_conn() { n sh -c "echo 1 > /sys/fs/fuse/connections/$1/abort"; }
    remount() { "$ROOT/scripts/vm.sh" mount; }
else
    CLOG=$ROOT/state/log/ceph-client.galaxy-node.log
    NODEIP_HEX=0x0ab14d1f   # 10.177.77.31 (cfr-node)
    n() { docker exec cfr-node "$@"; }
    fusectl() { cat "/sys/fs/fuse/connections/$(n stat -c %d /mnt/scratch)/$1"; }
    abort_conn() { echo 1 >"/sys/fs/fuse/connections/$1/abort"; }
    remount() { "$ROOT/scripts/cluster.sh" mount; }
fi
h() { docker exec cfr-head "$@"; }
asok() { n ceph --admin-daemon /var/run/ceph/ceph-client.galaxy-node.asok "$@"; }
ino() { printf '%x' "$(h stat -c %i "$1")"; }
primary() { # file on head -> primary osd of its first object
    docker exec cfr-mon ceph osd map cephfs.cephfs.data "$(ino "$1").00000000" -f json |
        python3 -c 'import json,sys;print(json.load(sys.stdin)["acting_primary"])'
}
newlog() { tail -n +$mark "$CLOG"; }
PROBE='label=$1; shift
"$@" >/dev/null 2>&1 & p=$!
for i in $(seq 20); do kill -0 $p 2>/dev/null || break; sleep 0.5; done
if kill -0 $p 2>/dev/null; then echo "$label: HUNG >10 s  wchan=$(cat /proc/$p/wchan)"; kill -9 $p
else wait $p; echo "$label: ok (rc=$?)"; fi'
probe() { n bash -c "$PROBE" probe "$@"; }

t0=$(date +%s.%N)
echo "== $(date -u +%FT%TZ) mode=$mode churn=$churn files=$nfiles stage $STAGE kernel $(n uname -r), $(n ceph-fuse --version | head -1)"
# The held OSD is osd.1; files are created until N of them have their (only)
# object there, churn files only on the other OSDs.
h sh -c "rm -rf $D; mkdir -p $D/a; head -c 64k /dev/urandom > $D/W; head -c 1M /dev/urandom > $D/fresh; head -c 1M /dev/urandom > $D/fresh2; sync"
osd=1
targets=(); churnfiles=(); i=0
while [ ${#targets[@]} -lt "$nfiles" ] || [ ${#churnfiles[@]} -lt 12 ]; do
    i=$((i + 1)); f=$D/a/f$i
    h sh -c "head -c 4M /dev/urandom > $f"
    p=$(primary "$f")
    if [ "$p" = $osd ] && [ ${#targets[@]} -lt "$nfiles" ]; then targets+=("$f")
    elif [ "$p" != $osd ] && [ ${#churnfiles[@]} -lt 12 ]; then
        h sh -c "truncate -s 512k $f"; churnfiles+=("$f")
    fi
done
h sync
# inode numbers now: once a read is lost, stat from the other client blocks
# on the caps the node can no longer release
objs=$(for f in "${targets[@]}"; do echo "$(ino "$f").00000000"; done)
h sync
echo "== ${#targets[@]} target file(s) on osd.$osd, ${#churnfiles[@]} churn files (512 KiB) elsewhere"

asok config set client_oc_size 2097152 >/dev/null
asok config set debug_objectcacher 10 >/dev/null
asok config set debug_client 10 >/dev/null
# the first data access runs Client::check_pool_perm (stat + create on the
# file's first object); do it now on another file, not while replies are held
n cat $D/W >/dev/null
n stat "${targets[@]}" >/dev/null
client_id=$(asok status | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
mark=$(($(wc -l <"$CLOG") + 1))

docker exec cfr-osd$osd sh -c "tc qdisc add dev eth0 root handle 1: prio && \
  tc filter add dev eth0 parent 1: protocol ip prio 1 basic match 'cmp(u32 at 16 layer network eq $NODEIP_HEX)' action drop"
trap 'docker exec cfr-osd$osd tc qdisc del dev eth0 root 2>/dev/null' EXIT
echo "== $(date +%T) replies from osd.$osd to node held"

n sh -c "rm -f /tmp/oclost.*; for f in ${targets[*]}; do setsid -f sh -c \"dd if=\$f of=/dev/null bs=64k count=1 2>/dev/null; echo \$f >> /tmp/oclost.done\" </dev/null >/dev/null 2>&1; done"
sleep 4
echo "== in flight: $(asok objecter_requests | python3 -c 'import json,sys;o=json.load(sys.stdin)["ops"];print(len(o), "ops to osd", sorted({x["osd"] for x in o}))'), fusectl waiting=$(fusectl waiting)"

for f in "${targets[@]}"; do
    case $mode in
        punch) n sh -c "setsid -f fallocate -p -o 0 -l 4194304 $f </dev/null >>/tmp/oclost.disc 2>&1" ;;
        truncate) n sh -c "setsid -f truncate -s 0 $f </dev/null >>/tmp/oclost.disc 2>&1" ;;
    esac
done
sleep 4
disc=$(newlog | grep -cE "discard object.* bh bh\[.* rx\] waiters = \{ ")
echo "== discards that hit an rx buffer with a waiter: $disc"
newlog | grep -E "discard object.* bh bh\[.* rx\] waiters" | head -3 | cut -c1-260

if [ "$churn" = wchurn ]; then
    # Write path, for when the background slots are already full (reads, even
    # with POSIX_FADV_RANDOM, go through ->readahead and queue behind them):
    # a committed write leaves a clean buffer at the top of the LRU; with a
    # 4 KiB cache the next writex() trim() expires everything below it.
    n dd if=/dev/urandom of="${churnfiles[0]}" bs=64k count=1 conv=notrunc,fsync status=none
    asok config set client_oc_size 4096 >/dev/null
    n dd if=/dev/urandom of="${churnfiles[1]}" bs=4k count=1 conv=notrunc status=none
    asok config set client_oc_size 2097152 >/dev/null
    echo "== write churn: committed 64 KiB, then one write with a 4 KiB cache"
elif [ "$churn" = churn ]; then
    # POSIX_FADV_RANDOM: no kernel readahead, so these are synchronous FUSE
    # reads that do not queue behind held background (readahead) requests
    n python3 -c 'import os,sys
for f in sys.argv[1:]:
    fd=os.open(f,os.O_RDONLY); os.posix_fadvise(fd,0,0,os.POSIX_FADV_RANDOM)
    while os.read(fd,65536): pass
    os.close(fd)' "${churnfiles[@]}"
    echo "== churned $(( ${#churnfiles[@]} * 512 )) KiB through the 2 MiB cache"
fi
sleep 2
trims=$(newlog | grep -cE "trim trimming bh\[.* zero\] waiters = \{ [0-9]")
echo "== zero buffers trimmed with waiters still attached: $trims"
newlog | grep -E "trim trimming bh\[.* zero\] waiters = \{ [0-9]" | head -3 | cut -c1-260

docker exec cfr-osd$osd tc qdisc del dev eth0 root
trap - EXIT
echo "== $(date +%T) replies released"
done_n=0
for i in $(seq 60); do
    done_n=$(n sh -c "cat /tmp/oclost.done 2>/dev/null | wc -l")
    [ "$done_n" -ge "$nfiles" ] && break
    sleep 1
done
if [ "$done_n" -ge "$nfiles" ]; then
    outcome="all $nfiles read(s) completed after release"
else
    outcome="READ HUNG: $((nfiles - done_n)) of $nfiles read(s) never returned (60 s after release)"
fi
echo "== $outcome"
echo "== objecter ops left: $(asok objecter_requests | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["ops"]))'), fusectl waiting=$(fusectl waiting) max_background=$(fusectl max_background)"
newlog | grep -E "bh_read_finish [0-9a-f]+\.00000000/head tid" | grep -F "$objs" | head -3 | cut -c1-200
n sh -c 'for p in $(pgrep -x dd; pgrep -x fallocate; pgrep -x truncate); do echo "  $p $(cat /proc/$p/comm) $(cut -d")" -f2 /proc/$p/stat | cut -d" " -f2) $(cat /proc/$p/wchan)"; done'
n sh -c 'p=$(pgrep -x ceph-fuse); for t in /proc/$p/task/*; do s=$(cut -d")" -f2 $t/stat | cut -d" " -f2); [ "$s" = D ] && echo "  ceph-fuse thread $(cat $t/comm) D $(cat $t/wchan)"; done; true'

if [[ $outcome == READ* ]]; then
    echo "== probes on the wedged node:"
    probe "stat                     " stat $D/fresh
    probe "O_DIRECT read            " dd if=$D/fresh of=/dev/null bs=1M iflag=direct
    probe "buffered read, uncached  " cat $D/fresh2
    probe "write                    " sh -c "head -c 64k /dev/urandom > $D/own"
    timeout 15 docker exec cfr-head cat "${targets[0]}" >/dev/null 2>&1 && echo "other client reading target: ok" ||
        echo "other client reading target: HUNG >15 s"
    if [ "$STAGE" = B ]; then
        echo "== kernel stacks of D-state tasks in the guest:"
        n sh -c 'for t in /proc/[0-9]*/task/[0-9]*; do [ "$(cut -d")" -f2 $t/stat 2>/dev/null | cut -d" " -f2)" = D ] || continue; echo "-- $(cat $t/comm) (${t#/proc/})"; cat $t/stack; done' | tee "$out/dstacks.txt"
        echo "== waiting for the hung-task detector (120 s)"
        for i in $(seq 40); do n dmesg | grep -q "blocked for more than" && break; sleep 5; done
        n dmesg >"$out/guest-dmesg.txt"
        grep -A25 "blocked for more than" "$out/guest-dmesg.txt" | grep -E "blocked for|Call Trace|^\[.*\]  [a-z_]+" | head -40 | cut -c1-120
    fi
    CFR_STAGE=$STAGE "$ROOT/harness/capture.sh" "$out/capture" oclost >/dev/null 2>&1 || true
fi
newlog >"$out/client.log"
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(basename "$out")" "$mode" "$churn" "$nfiles" "$disc" "$trims" "$outcome" >"$out/summary.tsv"
echo "$outcome" >"$out/outcome"

if [[ $outcome == READ* ]]; then
    echo "== recovering: kill readers, abort the connection, evict the node session, remount"
    # Killing ceph-fuse is not enough: its fn_anonymous thread sits in the
    # kernel invalidate on the folio the lost read keeps locked, so the process
    # never exits and /dev/fuse stays open. Abort the connection via fusectl.
    conn=$(n stat -c %d /mnt/scratch)
    n sh -c 'pkill -9 -x ceph-fuse'; sleep 1
    abort_conn "$conn"
    n sh -c 'pkill -9 -f "[c]oreutils-prog-shebang=(dd|cat|truncate)"; pkill -9 -x dd; pkill -9 -x fallocate; pkill -9 -x truncate; pkill -9 -x cat; pkill -9 -x ceph-fuse; sleep 1; umount -l /mnt/scratch' || true
    docker exec cfr-mon ceph tell mds.0 client evict id="$client_id" >/dev/null 2>&1 || true
    docker exec cfr-head sh -c 'pkill -9 -f "[c]oreutils-prog-shebang=(dd|cat)"; pkill -9 -x cat; pkill -9 -x dd' || true
    remount >/dev/null && echo "== node remounted"
else
    asok config set debug_objectcacher 0/5 >/dev/null
    asok config set debug_client 0/5 >/dev/null
    asok config set client_oc_size 209715200 >/dev/null
fi
echo "== elapsed $(python3 -c "import time;print(round(time.time()-$t0))") s"
