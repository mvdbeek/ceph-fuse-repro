# qemu/KVM runner for the Stage B VM. The VM runs inside this container so
# that its user-mode network can reach the rootless-docker "cfr" network.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils cloud-image-utils virtiofsd openssh-client ca-certificates iproute2 \
 && rm -rf /var/lib/apt/lists/*
