#!/usr/bin/env bash
# Bring a minimal ceph 17.2.9 cluster (1 mon, 1 mgr, 3 bluestore OSDs, 1 MDS,
# one CephFS with a Manila-style subvolume) and two ceph-fuse clients up or
# down with plain `docker run`. Works with rootless docker.
#
#   scripts/cluster.sh up | down | status | mount | umount
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${IMAGE:-ceph-fuse-repro:17.2.9}
NET=${NET:-cfr}
SUBNET=${SUBNET:-10.177.77.0/24}
MON_IP=10.177.77.10
STATE=${STATE:-$ROOT/state}
SUBVOL=${SUBVOL:-9b3c1f0e-5a7d-4c2e-8f61-2d4b6a0c9e11}
CLIENTS=(head node)
# Memory caps (chonk is shared; total stays under ~6 GB).
MEM_MON=384m MEM_MGR=640m MEM_OSD=768m MEM_MDS=512m MEM_CLIENT=1g

log() { echo "[cluster] $*" >&2; }

run_daemon() { # name ip mem cmd...
    local name=$1 ip=$2 mem=$3; shift 3
    docker run -d --name "cfr-$name" --hostname "$name" --network "$NET" --ip "$ip" \
        --memory "$mem" --memory-swap "$mem" --privileged --init -e CEPH_USE_RANDOM_NONCE=1 \
        -v "$STATE/etc:/etc/ceph" -v "$STATE/lib:/var/lib/ceph" -v "$STATE/log:/var/log/ceph" \
        -v "$STATE/run/$name:/var/run/ceph" \
        --entrypoint "$1" "$IMAGE" "${@:2}" >/dev/null
}

cexec() { docker exec "cfr-$1" "${@:2}"; }
ceph_admin() { cexec mon ceph "$@"; }

wait_for() { # description timeout cmd...
    local what=$1 t=$2; shift 2
    for ((i = 0; i < t; i++)); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    log "timed out waiting for $what"; return 1
}

write_conf() {
    local fsid=$1
    cat >"$STATE/etc/ceph.conf" <<EOF
[global]
fsid = $fsid
mon_host = $MON_IP
mon_initial_members = a
public_network = $SUBNET
auth_cluster_required = cephx
auth_service_required = cephx
auth_client_required = cephx
osd_pool_default_size = 3
osd_pool_default_min_size = 2
osd_pool_default_pg_num = 32
mon_allow_pool_delete = true
mon_warn_on_pool_no_redundancy = false
log_file = /var/log/ceph/\$cluster-\$name.log
admin_socket = /var/run/ceph/\$cluster-\$name.asok

[mon]
mon_memory_target = 268435456
mon_data_avail_warn = 2

[osd]
osd_objectstore = bluestore
bluestore_block_create = true
bluestore_block_size = 8589934592
osd_memory_target = 536870912
osd_memory_base = 268435456
osd_memory_cache_min = 67108864
bluestore_cache_autotune = true

[mds]
mds_cache_memory_limit = 268435456

[client]
# ceph-fuse defaults, as in production (fuse_use_invalidate_cb = true).
# harness/run.py --faults debugclient raises debug_client at runtime.
EOF
}

up() {
    if docker container inspect cfr-mon >/dev/null 2>&1; then
        log "already up (cfr-mon exists); run 'down' first"; exit 1
    fi
    docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -t "$IMAGE" "$ROOT/docker"
    docker network inspect "$NET" >/dev/null 2>&1 || docker network create --subnet "$SUBNET" "$NET" >/dev/null

    mkdir -p "$STATE"/{etc,lib,log} "$STATE"/run/{mon,mgr,mds,osd0,osd1,osd2,head,node}
    local fresh=0
    if [[ ! -f $STATE/etc/ceph.conf ]]; then
        fresh=1
        local fsid; fsid=$(cat /proc/sys/kernel/random/uuid)
        write_conf "$fsid"
        log "bootstrapping new cluster $fsid"
        docker run --rm --network "$NET" --ip $MON_IP -v "$STATE/etc:/etc/ceph" -v "$STATE/lib:/var/lib/ceph" \
            --entrypoint bash "$IMAGE" -euc "
            ceph-authtool --create-keyring /tmp/mon.keyring --gen-key -n mon. --cap mon 'allow *'
            ceph-authtool --create-keyring /etc/ceph/ceph.client.admin.keyring --gen-key -n client.admin \
                --cap mon 'allow *' --cap osd 'allow *' --cap mds 'allow *' --cap mgr 'allow *'
            ceph-authtool --create-keyring /etc/ceph/bootstrap-osd.keyring --gen-key -n client.bootstrap-osd \
                --cap mon 'profile bootstrap-osd' --cap mgr 'allow r'
            ceph-authtool /tmp/mon.keyring --import-keyring /etc/ceph/ceph.client.admin.keyring
            ceph-authtool /tmp/mon.keyring --import-keyring /etc/ceph/bootstrap-osd.keyring
            monmaptool --create --add a $MON_IP --fsid $fsid /tmp/monmap
            mkdir -p /var/lib/ceph/mon/ceph-a
            ceph-mon --mkfs -i a --monmap /tmp/monmap --keyring /tmp/mon.keyring
            chmod -R a+rX /etc/ceph"
    fi

    log "starting mon"
    run_daemon mon $MON_IP $MEM_MON ceph-mon -f -i a --public-addr $MON_IP
    wait_for "mon quorum" 60 ceph_admin -s

    if ((fresh)); then
        ceph_admin mon enable-msgr2
        ceph_admin config set mon auth_allow_insecure_global_id_reclaim false
        ceph_admin auth get-or-create mgr.x mon 'allow profile mgr' osd 'allow *' mds 'allow *' \
            -o /etc/ceph/mgr.keyring
        ceph_admin auth get-or-create mds.a mon 'allow profile mds' osd 'allow rwx' mds 'allow *' mgr 'allow profile mds' \
            -o /etc/ceph/mds.keyring
        cexec mon bash -c 'mkdir -p /var/lib/ceph/mgr/ceph-x /var/lib/ceph/mds/ceph-a
            cp /etc/ceph/mgr.keyring /var/lib/ceph/mgr/ceph-x/keyring
            cp /etc/ceph/mds.keyring /var/lib/ceph/mds/ceph-a/keyring'
    fi

    if ((!fresh)); then
        # A restarted OSD can come back with its old address (pid nonce), which
        # the mon ignores as "already up"; mark them down so they re-boot.
        ceph_admin osd down 0 1 2 >/dev/null 2>&1 || true
    fi
    log "starting mgr"
    run_daemon mgr 10.177.77.11 $MEM_MGR ceph-mgr -f -i x
    for i in 0 1 2; do
        if [[ ! -f $STATE/lib/osd/ceph-$i/keyring ]]; then
            log "creating osd.$i"
            docker run --rm --network "$NET" -v "$STATE/etc:/etc/ceph" -v "$STATE/lib:/var/lib/ceph" \
                --entrypoint bash "$IMAGE" -euc "
                uuid=\$(cat /proc/sys/kernel/random/uuid)
                key=\$(ceph-authtool --gen-print-key)
                id=\$(echo \"{\\\"cephx_secret\\\": \\\"\$key\\\"}\" | ceph -n client.bootstrap-osd \
                    -k /etc/ceph/bootstrap-osd.keyring osd new \$uuid -i -)
                [ \"\$id\" = $i ] || { echo \"got osd id \$id, expected $i\"; exit 1; }
                d=/var/lib/ceph/osd/ceph-$i; mkdir -p \$d
                ceph-authtool --create-keyring \$d/keyring --name osd.$i --add-key \$key
                ceph-osd -i $i --mkfs --osd-uuid \$uuid --osd-data \$d"
        fi
        log "starting osd.$i"
        run_daemon osd$i 10.177.77.2$i $MEM_OSD ceph-osd -f -i $i
    done
    log "starting mds"
    run_daemon mds 10.177.77.12 $MEM_MDS ceph-mds -f -i a

    if ((fresh)); then
        wait_for "mgr" 120 bash -c "docker exec cfr-mon ceph mgr stat | grep -q '\"available\": true'"
        ceph_admin fs volume create cephfs
        wait_for "mds active" 120 bash -c "docker exec cfr-mon ceph mds stat | grep -q up:active"
        ceph_admin fs subvolume create cephfs "$SUBVOL"
        for c in "${CLIENTS[@]}"; do
            # Manila-style per-subvolume cephx id (production: client.galaxy-scratch)
            ceph_admin fs subvolume authorize cephfs "$SUBVOL" "galaxy-$c" --access_level=rw
            ceph_admin auth get "client.galaxy-$c" -o "/etc/ceph/ceph.client.galaxy-$c.keyring"
        done
        ceph_admin fs subvolume getpath cephfs "$SUBVOL" | tr -d '\n' >"$STATE/etc/subvol_path"
        chmod a+r "$STATE"/etc/*
    fi
    wait_for "mds active" 180 bash -c "docker exec cfr-mon ceph mds stat | grep -q up:active"
    wait_for "HEALTH_OK/active+clean" 180 bash -c "docker exec cfr-mon ceph pg stat | grep -qv -E 'peering|creating|unknown|activating' && docker exec cfr-mon ceph pg stat | grep -q active+clean"

    for i in "${!CLIENTS[@]}"; do
        local c=${CLIENTS[$i]}
        log "starting client container $c"
        docker run -d --name "cfr-$c" --hostname "$c" --network "$NET" --ip "10.177.77.3$i" \
            --memory $MEM_CLIENT --memory-swap $MEM_CLIENT --privileged --init --device /dev/fuse -e CEPH_USE_RANDOM_NONCE=1 \
            -v "$STATE/etc:/etc/ceph:ro" -v "$STATE/log:/var/log/ceph" -v "$STATE/run/$c:/var/run/ceph" \
            -v "$ROOT/harness:/harness:ro" -v "$STATE/ctl:/ctl" -v "$ROOT/results:/results" \
            -v /sys/fs/fuse/connections:/fusectl:ro \
            --entrypoint sleep "$IMAGE" infinity >/dev/null
    done
    mkdir -p "$STATE/ctl" "$ROOT/results"
    mount_clients
    status
}

mount_clients() {
    local path; path=$(cat "$STATE/etc/subvol_path")
    for c in "${CLIENTS[@]}"; do
        if cexec "$c" mountpoint -q /mnt/scratch; then continue; fi
        cexec "$c" mkdir -p /mnt/scratch
        # Same invocation as production, apart from the id and the mountpoint.
        cexec "$c" ceph-fuse --id="galaxy-$c" --conf=/etc/ceph/ceph.conf \
            --client_mountpoint="$path" /mnt/scratch "$@"
        wait_for "$c mount" 30 cexec "$c" mountpoint -q /mnt/scratch
        log "$c: mounted $path at /mnt/scratch"
    done
}

umount_clients() {
    for c in "${CLIENTS[@]}"; do
        cexec "$c" bash -c 'mountpoint -q /mnt/scratch && (umount /mnt/scratch || umount -l /mnt/scratch); pkill -x ceph-fuse; true' || true
    done
}

down() {
    umount_clients 2>/dev/null || true
    local cs; cs=$(docker ps -aq --filter name='^cfr-')
    [[ -n $cs ]] && docker rm -f $cs >/dev/null
    docker network rm "$NET" >/dev/null 2>&1 || true
    if [[ ${1:-} == --purge ]]; then
        log "purging $STATE"
        # Files are owned by host uid via rootless docker; remove via a container to be safe.
        docker run --rm -v "$ROOT:/r" --entrypoint rm "$IMAGE" -rf /r/state
    fi
    log "down"
}

status() {
    docker ps --filter name='^cfr-' --format '{{.Names}}\t{{.Status}}'
    ceph_admin -s 2>/dev/null || true
    docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' $(docker ps -q --filter name='^cfr-') 2>/dev/null || true
}

case ${1:-} in
    up) up ;;
    down) down "${2:-}" ;;
    status) status ;;
    mount) shift; mount_clients "$@" ;;
    umount) umount_clients ;;
    *) echo "usage: $0 up|down [--purge]|status|mount|umount" >&2; exit 2 ;;
esac
