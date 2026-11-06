#!/usr/bin/env bash
# Show what a client looks like when every FUSE background slot is taken by a
# request ceph-fuse cannot answer yet (H1's mechanism, made on purpose).
#
# 1. head writes N files the node has never read; node writes "own" in a
#    separate directory.
# 2. Pause the three OSD containers. Control: an O_DIRECT and a buffered read
#    of uncached files while the background slots are still free.
# 3. Start N buffered readers of the uncached files on node: their readahead
#    goes out as FUSE background requests that sit in ceph-fuse waiting on the
#    OSDs, until the slots (max_background) are used up.
# 4. Probe from node: stat, O_DIRECT read and buffered read of "own", and an
#    O_DIRECT read of an uncached file. Each probe reports, after 5 s, whether
#    it finished or its state, wchan and syscall; then it is killed.
# 5. Unpause the OSDs.
#
#   harness/h1_mechanics.sh [readers] [outdir]
set -uo pipefail
N=${1:-32}
out=${2:-results/h1-mechanics-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$out"
exec > >(tee "$out/log.txt") 2>&1
D=/mnt/scratch/h1mech

# Probe script run inside cfr-node: $1 = label, rest = command.
PROBE='label=$1; shift
"$@" >/dev/null 2>&1 & p=$!
sleep 5
if kill -0 $p 2>/dev/null; then
    st=$(cut -d")" -f2 /proc/$p/stat | cut -d" " -f2)
    echo "$label: HUNG >5 s  state=$st wchan=$(cat /proc/$p/wchan) syscall=$(cut -d" " -f1 /proc/$p/syscall)"
    kill -9 $p
else
    wait $p; echo "$label: finished rc=$?"
fi'
probe() { docker exec cfr-node bash -c "$PROBE" probe "$@"; }
echo "(syscall numbers: 0=read 17=pread64 257=openat 5=fstat 4=stat 262=newfstatat)"

docker exec cfr-head bash -c "rm -rf $D $D.own; mkdir -p $D; for i in \$(seq 1 $N); do head -c 4M /dev/urandom > $D/f\$i; done; sync"
# "own" lives in its own directory so its lookups never queue behind the readers' lookups.
docker exec cfr-node bash -c "mkdir -p $D.own; head -c 4M /dev/urandom > $D.own/own; sync; md5sum $D.own/own; ls $D | wc -l"
echo "== kernel $(docker exec cfr-node uname -r), $(docker exec cfr-node ceph-fuse --version)"

unpause() { docker unpause cfr-osd0 cfr-osd1 cfr-osd2 >/dev/null 2>&1; }
trap unpause EXIT
docker pause cfr-osd0 cfr-osd1 cfr-osd2 >/dev/null
echo "== OSDs paused, background slots still free (control)"
probe "O_DIRECT f2  (uncached, free slots)" dd if=$D/f2 of=/dev/null bs=1M iflag=direct
probe "buffered f3  (uncached, free slots)" cat $D/f3

echo "== starting $N buffered readers of uncached files"
docker exec -d cfr-node bash -c "for i in \$(seq 4 $N); do cat $D/f\$i > /dev/null & done; wait"
sleep 8
echo "== reader tasks: count, state, wchan (after 8 s)"
docker exec cfr-node bash -c 'for p in $(pgrep -x cat); do echo "$(cut -d")" -f2 /proc/$p/stat | cut -d" " -f2) $(cat /proc/$p/wchan)"; done | sort | uniq -c'

probe "stat own              " stat $D.own/own
probe "O_DIRECT own          " dd if=$D.own/own of=/dev/null bs=1M iflag=direct
probe "buffered own (no cache)" python3 -c "import os; f=os.open('$D.own/own', os.O_RDONLY); os.posix_fadvise(f,0,0,os.POSIX_FADV_DONTNEED); [0 for _ in iter(lambda: os.read(f, 1<<20), b'')]"
probe "O_DIRECT f1 (uncached)" dd if=$D/f1 of=/dev/null bs=1M iflag=direct
echo "== ceph-fuse in-flight OSD ops: $(docker exec cfr-node ceph --admin-daemon /var/run/ceph/ceph-client.galaxy-node.asok objecter_requests | grep -c '"tid"')"

unpause
trap - EXIT
echo "== OSDs unpaused"
sleep 10
docker exec cfr-node bash -c 'echo "readers left: $(pgrep -xc cat)"'
probe "buffered own after unpause" cat $D.own/own
docker exec cfr-head rm -rf $D $D.own
