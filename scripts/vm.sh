#!/usr/bin/env bash
# Stage B: a Rocky 9 KVM guest with the production node's kernel
# (elrepo kernel-lt 6.1.188) and ceph-fuse 17.2.9, as the "node" client of the
# Stage A cluster. qemu runs inside the cfr-vm container on the "cfr" network,
# so the guest's user-mode (slirp) network reaches the cluster; host
# directories reach the guest over virtiofs at /share.
#
#   scripts/vm.sh up | down | ssh [cmd...] | mount | umount | console | status
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=${STATE:-$ROOT/state}
VM=$STATE/vm
NET=${NET:-cfr}
BASE_URL=https://download.rockylinux.org/pub/rocky/9/images/x86_64
BASE=Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2
KVER=6.1.188-1.el9.elrepo
KURL=https://mirrors.coreix.net/elrepo-archive-archive/kernel/el9/x86_64/RPMS
CEPH_VERSION=17.2.9
VM_MEM_MB=${VM_MEM_MB:-3072}
VM_CPUS=${VM_CPUS:-4}

log() { echo "[vm] $*" >&2; }

guest() { # run a command in the guest as root
    docker exec -i cfr-vm ssh -i /vm/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=5 -p 2222 root@127.0.0.1 "$(printf '%q ' "$@")"
}

user_data() {
    cat <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys: ["$(cat "$VM/id_ed25519.pub")"]
mounts:
  - [share, /share, virtiofs, "defaults,nofail", "0", "0"]
write_files:
  - path: /etc/yum.repos.d/ceph.repo
    content: |
      [ceph]
      name=ceph
      baseurl=https://download.ceph.com/rpm-$CEPH_VERSION/el9/\$basearch
      gpgcheck=1
      gpgkey=https://download.ceph.com/keys/release.asc
      [ceph-noarch]
      name=ceph-noarch
      baseurl=https://download.ceph.com/rpm-$CEPH_VERSION/el9/noarch
      gpgcheck=1
      gpgkey=https://download.ceph.com/keys/release.asc
  - path: /etc/sysctl.d/90-cfr.conf
    content: |
      # report every hung task, not just the first 10 (production ran out)
      kernel.hung_task_warnings = -1
runcmd:
  - [sh, -xc, "mkdir -p /share && (mountpoint -q /share || mount -t virtiofs share /share)"]
  - [sh, -xc, "dnf -y install epel-release dnf-plugins-core && dnf config-manager --set-enabled crb"]
  - [sh, -xc, "dnf -y install ceph-fuse-$CEPH_VERSION ceph-common-$CEPH_VERSION python3 psmisc procps-ng util-linux"]
  - [sh, -xc, "dnf -y install $KURL/kernel-lt-$KVER.x86_64.rpm $KURL/kernel-lt-core-$KVER.x86_64.rpm $KURL/kernel-lt-modules-$KVER.x86_64.rpm"]
  - [sh, -xc, "grubby --set-default /boot/vmlinuz-$KVER.x86_64"]
  - [sh, -xc, "ln -sfn /share/harness /harness; ln -sfn /share/ctl /ctl; ln -sfn /share/results /results; mkdir -p /var/log/ceph /var/run/ceph"]
  - [sh, -xc, "touch /var/lib/cfr-provisioned"]
power_state:
  mode: reboot
  condition: true
EOF
}

prepare() {
    mkdir -p "$VM" "$STATE/log-vm"
    if [[ ! -f $VM/$BASE ]]; then
        log "downloading $BASE"
        curl -fL -o "$VM/$BASE.part" "$BASE_URL/$BASE"
        mv "$VM/$BASE.part" "$VM/$BASE"
    fi
    [[ -f $VM/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -f "$VM/id_ed25519"
    user_data >"$VM/user-data"
    printf 'instance-id: cfr-vm-1\nlocal-hostname: node-vm\n' >"$VM/meta-data"
    docker image inspect cfr-qemu >/dev/null 2>&1 || docker build -t cfr-qemu "$ROOT/docker/qemu"
    docker run --rm -v "$VM:/vm" cfr-qemu sh -ec '
        cloud-localds /vm/seed.iso /vm/user-data /vm/meta-data
        [ -f /vm/disk.qcow2 ] || qemu-img create -q -f qcow2 -b '"$BASE"' -F qcow2 /vm/disk.qcow2 20G'
}

up() {
    if docker container inspect cfr-vm >/dev/null 2>&1; then log "cfr-vm exists"; exit 1; fi
    prepare
    cat >"$VM/run-vm.sh" <<EOF
#!/bin/sh
set -e
/usr/libexec/virtiofsd --socket-path=/tmp/vfs.sock --shared-dir=/share --cache=never --sandbox=none \
    --inode-file-handles=never --log-level=error &
while [ ! -S /tmp/vfs.sock ]; do sleep 0.2; done
exec qemu-system-x86_64 -name node-vm -machine q35,accel=kvm -cpu host -smp $VM_CPUS -m ${VM_MEM_MB}M \
    -object memory-backend-memfd,id=mem,size=${VM_MEM_MB}M,share=on -numa node,memdev=mem \
    -chardev socket,id=vfs,path=/tmp/vfs.sock -device vhost-user-fs-pci,chardev=vfs,tag=share \
    -drive file=/vm/disk.qcow2,if=virtio,cache=unsafe -drive file=/vm/seed.iso,if=virtio,media=cdrom \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=n0 \
    -display none -serial file:/vm/console.log -monitor unix:/tmp/qemu-monitor.sock,server,nowait
EOF
    chmod +x "$VM/run-vm.sh"
    log "starting cfr-vm (guest ${VM_MEM_MB} MB, $VM_CPUS vCPUs)"
    docker run -d --name cfr-vm --hostname vmhost --network "$NET" --ip 10.177.77.40 \
        --device /dev/kvm --memory $((VM_MEM_MB + 512))m --memory-swap $((VM_MEM_MB + 512))m \
        -v "$VM:/vm" -v "$ROOT/harness:/share/harness:ro" -v "$STATE/ctl:/share/ctl" \
        -v "$ROOT/results:/share/results" -v "$STATE/etc:/share/etc:ro" -v "$STATE/log-vm:/share/log" \
        --entrypoint /vm/run-vm.sh cfr-qemu >/dev/null
    log "waiting for ssh (first boot provisions packages and reboots into $KVER; ~5 min)"
    for ((i = 0; i < 180; i++)); do
        if guest test -f /var/lib/cfr-provisioned 2>/dev/null && [[ $(guest uname -r 2>/dev/null) == "$KVER.x86_64" ]]; then
            log "guest up: $(guest uname -r), $(guest ceph-fuse --version)"
            setup_guest
            return
        fi
        sleep 5
    done
    log "guest not ready after 15 min; see $VM/console.log"; exit 1
}

setup_guest() {
    guest sh -ec '
        mountpoint -q /share || mount -t virtiofs share /share
        mkdir -p /etc/ceph /var/log/ceph /var/run/ceph
        cp /share/etc/ceph.conf /share/etc/ceph.client.galaxy-node.keyring /share/etc/subvol_path /etc/ceph/
        # same log location as the node container, but visible on the host as state/log-vm
        sed -i "s|^log_file = .*|log_file = /share/log/\$cluster-\$name.log|" /etc/ceph/ceph.conf
        sysctl -q kernel.hung_task_warnings=-1'
}

mount_node() {
    setup_guest
    guest sh -ec '
        mountpoint -q /mnt/scratch && exit 0
        mkdir -p /mnt/scratch
        ceph-fuse --id=galaxy-node --conf=/etc/ceph/ceph.conf --client_mountpoint="$(cat /etc/ceph/subvol_path)" /mnt/scratch
        mountpoint /mnt/scratch'
}

case ${1:-} in
    up) up ;;
    down)
        guest sh -c 'umount /mnt/scratch 2>/dev/null; poweroff' 2>/dev/null || true
        sleep 5; docker rm -f cfr-vm >/dev/null 2>&1 || true; log "down" ;;
    purge) docker rm -f cfr-vm >/dev/null 2>&1 || true; rm -f "$VM/disk.qcow2" "$VM/seed.iso"; log "purged guest disk (base image kept)" ;;
    ssh) shift; guest "$@" ;;
    mount) mount_node ;;
    umount) guest sh -c 'umount /mnt/scratch || umount -l /mnt/scratch; pkill -x ceph-fuse; true' ;;
    console) tail -n 50 "$VM/console.log" ;;
    status) docker ps --filter name=cfr-vm --format '{{.Names}}\t{{.Status}}'; guest sh -c 'uname -r; ceph-fuse --version; mountpoint /mnt/scratch' || true ;;
    *) echo "usage: $0 up|down|purge|ssh [cmd]|mount|umount|console|status" >&2; exit 2 ;;
esac
