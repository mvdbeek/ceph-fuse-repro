#!/usr/bin/env bash
# Build ceph-fuse 17.2.9 and unittest_object_cacher from the release tarball
# with patches/*.patch applied, in a memory-capped Rocky 9 container (the same
# OS and compiler as the el9 packages). --test-only applies just the test patch
# (0001), to show the unit test failing on the unfixed code. The result goes to state/patched,
# which `CFR_PATCHED=1 scripts/cluster.sh up` runs instead of the packaged
# ceph-fuse.
#
#   scripts/build-ceph-fuse.sh [--test-only]
#
# The source and build tree live in $BUILD_DIR (default ~/src/ceph-build); the
# first build takes about an hour, later ones only rebuild what changed.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=${STATE:-$ROOT/state}
BUILD_DIR=${BUILD_DIR:-$HOME/src/ceph-build}
CEPH_VERSION=17.2.9
SRC=$BUILD_DIR/ceph-$CEPH_VERSION
JOBS=${JOBS:-4}
MEM=${MEM:-6g}
patches=("$ROOT"/patches/*.patch)
[[ ${1:-} == --test-only ]] && patches=("$ROOT"/patches/0001-*.patch)

log() { echo "[build] $*" >&2; }

mkdir -p "$BUILD_DIR"
if [[ ! -d $SRC.orig ]]; then
    tarball=$BUILD_DIR/ceph-$CEPH_VERSION.tar.gz
    if [[ ! -f $tarball ]]; then
        log "fetching ceph-$CEPH_VERSION.tar.gz"
        curl -fL -o "$tarball.part" "https://download.ceph.com/tarballs/ceph-$CEPH_VERSION.tar.gz"
        mv "$tarball.part" "$tarball"
    fi
    rm -rf "$BUILD_DIR/unpack"; mkdir "$BUILD_DIR/unpack"
    tar -C "$BUILD_DIR/unpack" -xzf "$tarball"
    mv "$BUILD_DIR/unpack/ceph-$CEPH_VERSION" "$SRC.orig"; rmdir "$BUILD_DIR/unpack"
fi
[[ -d $SRC ]] || cp -a "$SRC.orig" "$SRC"

# Reset the files the patches touch (with a new mtime, so ninja rebuilds them),
# then apply the patches (or not).
for p in "$ROOT"/patches/*.patch; do
    for f in $(sed -n 's|^+++ b/||p' "$p"); do
        if [[ -f $SRC.orig/$f ]]; then cp "$SRC.orig/$f" "$SRC/$f"; else rm -f "$SRC/$f"; fi
    done
done
for p in "${patches[@]}"; do
    log "applying $(basename "$p")"
    patch -s -d "$SRC" -p1 <"$p"
done

if ! docker container inspect cfr-build >/dev/null 2>&1; then
    log "starting build container (memory $MEM)"
    docker run -d --name cfr-build --memory "$MEM" --memory-swap "$MEM" --cpus 6 \
        -v "$BUILD_DIR:/build" quay.io/rockylinux/rockylinux:9 sleep infinity >/dev/null
    docker exec cfr-build sh -ec '
        dnf -y -q install epel-release dnf-plugins-core
        dnf config-manager --set-enabled crb
        dnf -y -q install gcc-c++ cmake ninja-build make git ccache patch perl which hostname procps-ng \
          python3-devel python3-setuptools python3-Cython python3-pyyaml python3-jinja2 \
          fuse-devel libaio-devel libblkid-devel libcap-ng-devel keyutils-libs-devel openssl-devel nss-devel \
          libcurl-devel expat-devel libuuid-devel snappy-devel lz4-devel zlib-devel bzip2-devel libzstd-devel \
          lua-devel libicu-devel gperf ragel cryptsetup-devel libnl3-devel numactl-devel systemd-devel \
          libudev-devel sqlite-devel ncurses-devel openldap-devel liboath-devel gperftools-devel fmt-devel \
          re2-devel libtool thrift-devel c-ares-devel systemtap-sdt-devel nasm libibverbs-devel \
          librdmacm-devel rdma-core-devel CUnit-devel xfsprogs-devel libxml2-devel >/dev/null'
fi

B=/build/ceph-$CEPH_VERSION/build
if ! docker exec cfr-build test -f $B/build.ninja; then
    log "configuring"
    docker exec cfr-build sh -ec "mkdir -p $B && cd $B && cmake -G Ninja .. -DCMAKE_BUILD_TYPE=RelWithDebInfo -DWITH_CCACHE=ON \
        -DWITH_MGR=OFF -DWITH_MGR_DASHBOARD_FRONTEND=OFF -DWITH_RADOSGW=OFF -DWITH_RBD=OFF -DWITH_KRBD=OFF \
        -DWITH_LTTNG=OFF -DWITH_BABELTRACE=OFF -DWITH_MANPAGE=OFF -DWITH_CEPHFS_JAVA=OFF -DWITH_RDMA=OFF \
        -DWITH_SPDK=OFF -DWITH_LIBCEPHSQLITE=OFF -DWITH_CEPHFS_SHELL=OFF -DWITH_SYSTEMD=OFF -DWITH_FUSE=ON \
        -DWITH_TESTS=ON -DWITH_RBD_RWL=OFF -DWITH_RBD_SSD_CACHE=OFF -DWITH_JAEGER=OFF -DWITH_GRAFANA=OFF \
        -DWITH_ZBD=OFF -DWITH_OPENLDAP=OFF -DWITH_LIBRADOSSTRIPER=OFF -DWITH_SEASTAR=OFF \
        -DWITH_RADOSGW_AMQP_ENDPOINT=OFF -DWITH_RADOSGW_KAFKA_ENDPOINT=OFF -DWITH_PYTHON3=3.9 \
        -DWITH_BLUESTORE=OFF >/build/cmake.log"
fi

log "building ceph-fuse and unittest_object_cacher (-j$JOBS)"
docker exec cfr-build sh -c "cd $B && ninja -j$JOBS ceph-fuse unittest_object_cacher"

log "running unittest_object_cacher"
docker exec cfr-build sh -c "cd $B && bin/unittest_object_cacher" || log "unittest_object_cacher FAILED"

mkdir -p "$STATE/patched/bin" "$STATE/patched/lib"
cp "$SRC/build/bin/ceph-fuse" "$STATE/patched/bin/"
cp -L "$SRC/build/lib/libceph-common.so.2" "$STATE/patched/lib/"
# built against the system fmt; the el9 packages bundle theirs, so ship it along
docker exec cfr-build sh -c 'cat $(ldconfig -p | sed -n "s|.*libfmt.so.8 .*=> ||p" | head -1)' >"$STATE/patched/lib/libfmt.so.8"
printf '%s\n' "${patches[@]##*/}" >"$STATE/patched/VERSION"
log "installed to $STATE/patched ($(tr '\n' ' ' <"$STATE/patched/VERSION"))"
