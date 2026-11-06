# Ceph 17.2.9 (quincy) on Rocky 9, the production node's OS.
# quay.io/ceph/ceph has no v17.2.9 tag (it stops at v17.2.8), so we build from
# the download.ceph.com el9 packages.
FROM quay.io/rockylinux/rockylinux:9
ARG CEPH_VERSION=17.2.9
RUN dnf -y install epel-release dnf-plugins-core \
 && dnf config-manager --set-enabled crb \
 && printf '[ceph]\nname=ceph\nbaseurl=https://download.ceph.com/rpm-%s/el9/$basearch\ngpgcheck=1\ngpgkey=https://download.ceph.com/keys/release.asc\n[ceph-noarch]\nname=ceph-noarch\nbaseurl=https://download.ceph.com/rpm-%s/el9/noarch\ngpgcheck=1\ngpgkey=https://download.ceph.com/keys/release.asc\n' "$CEPH_VERSION" "$CEPH_VERSION" > /etc/yum.repos.d/ceph.repo \
 && dnf -y install ceph-mon-$CEPH_VERSION ceph-mgr-$CEPH_VERSION ceph-osd-$CEPH_VERSION \
      ceph-mds-$CEPH_VERSION ceph-fuse-$CEPH_VERSION ceph-common-$CEPH_VERSION \
      iproute iproute-tc procps-ng psmisc python3 util-linux findutils jq \
 && dnf clean all
