#!/usr/bin/env bash
# Environmental alternative to a client bug: a path-MTU black hole between ONE
# OSD and the node client. Small packets (requests, acks) pass; data replies
# larger than 1400 bytes from osd.0 to the node are dropped silently, so TCP
# keeps retransmitting and the connection does not fail.
#
# Stage B (node = KVM guest, its traffic leaves via cfr-vm 10.177.77.40).
#   harness/blackhole.sh [outdir] [minutes_to_hold]
set -uo pipefail
out=${1:-results/blackhole-$(date +%Y%m%d-%H%M%S)}
hold=${2:-5}
mkdir -p "$out"
exec > >(tee "$out/log.txt") 2>&1
D=/mnt/scratch/bh
VMIP=10.177.77.40
g() { docker exec cfr-vm ssh -i /vm/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -p 2222 root@127.0.0.1 "$(printf '%q ' "$@")"; }
PROBE='label=$1; shift
"$@" >/dev/null 2>&1 & p=$!
sleep 10
if kill -0 $p 2>/dev/null; then
    echo "$label: HUNG >10 s  state=$(cut -d")" -f2 /proc/$p/stat | cut -d" " -f2) wchan=$(cat /proc/$p/wchan)"; kill -9 $p
else wait $p; echo "$label: ok (rc=$?)"; fi'
probe() { g bash -c "$PROBE" probe "$@"; }
asok() { g ceph --admin-daemon /var/run/ceph/ceph-client.galaxy-node.asok "$@"; }

echo "== $(date -u +%FT%TZ) kernel $(g uname -r)"
docker exec cfr-head bash -c "rm -rf $D; mkdir -p $D; for i in \$(seq 1 40); do head -c 4M /dev/urandom > $D/f\$i; done; sync"
declare -A prim
for i in $(seq 1 40); do
    ino=$(docker exec cfr-head stat -c %i $D/f$i)
    prim[$i]=$(docker exec cfr-mon ceph osd map cephfs.cephfs.data "$(printf '%x' "$ino").00000000" -f json | python3 -c 'import json,sys;print(json.load(sys.stdin)["acting_primary"])')
done
bad=(); good=()
for i in $(seq 1 40); do if [ "${prim[$i]}" = 0 ]; then bad+=("$i"); else good+=("$i"); fi; done
echo "== files with primary osd.0: ${#bad[@]} (${bad[*]}); others: ${#good[@]}"
fusectl() { g sh -c 'cd /sys/fs/fuse/connections/$(stat -c %d /mnt/scratch) && echo "waiting=$(cat waiting) max_background=$(cat max_background)"'; }
snapshot() {
    echo "== $1: $(fusectl)"
    g bash -c 'for p in $(pgrep -x cat); do echo "$(cut -d")" -f2 /proc/$p/stat | cut -d" " -f2) $(cat /proc/$p/wchan)"; done | sort | uniq -c'
    asok objecter_requests | python3 -c 'import json,sys,collections
o=json.load(sys.stdin)["ops"]; c=collections.Counter(x["osd"] for x in o)
print("   objecter ops by osd:", dict(c), "oldest age:", round(max([float(x["age"]) for x in o] or [0])), "s")'
}

# Black hole: drop >1400-byte packets from osd.0 to the VM node, nothing else.
docker exec cfr-osd0 sh -c "tc qdisc add dev eth0 root handle 1: prio && \
  tc filter add dev eth0 parent 1: protocol ip prio 1 basic match \
  'cmp(u32 at 16 layer network eq 0x0ab14d28) and meta(pkt_len gt 1400)' action drop"
docker exec cfr-osd0 tc filter show dev eth0 | head -3
trap 'docker exec cfr-osd0 tc qdisc del dev eth0 root 2>/dev/null' EXIT
echo "== $(date +%T) black hole on (osd.0 -> $VMIP, packets > 1400 bytes)"

g sh -c "setsid -f sh -c 'for i in \$(seq 1 40); do cat $D/f\$i > /dev/null & done; wait' </dev/null >/dev/null 2>&1"
sleep 60
snapshot "after 60 s"
probe "stat (MDS)                     " stat $D/f${good[0]}
probe "O_DIRECT, file on healthy OSD  " dd if=$D/f${good[1]} of=/dev/null bs=1M iflag=direct
probe "O_DIRECT, file on osd.0        " dd if=$D/f${bad[0]} of=/dev/null bs=1M iflag=direct
docker exec cfr-head bash -c "head -c 1M /dev/urandom > $D/fresh; sync"
probe "buffered, fresh file (any OSD) " cat $D/fresh
probe "write + read own file          " sh -c "head -c 64k /dev/urandom > $D/own && cat $D/own"
sleep $((hold * 60 - 60))
snapshot "after ${hold} min"
probe "buffered, fresh file (any OSD) " cat $D/fresh
docker exec cfr-mon ceph health detail | head -5

docker exec cfr-osd0 tc qdisc del dev eth0 root
trap - EXIT
t0=$(date +%s)
echo "== $(date +%T) black hole off"
for i in $(seq 120); do
    w=$(fusectl | sed -E 's/waiting=([0-9]+).*/\1/'); n=$(g pgrep -xc cat)
    [ "$w" = 0 ] && [ "${n:-0}" = 0 ] && break
    sleep 5
done
echo "== recovered after $(( $(date +%s) - t0 )) s: $(fusectl), readers left: $(g pgrep -xc cat)"
probe "buffered, fresh file after     " cat $D/fresh
docker exec cfr-head rm -rf $D
