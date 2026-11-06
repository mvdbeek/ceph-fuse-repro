# ceph-fuse stall reproducer

Trying to reproduce a ceph-fuse 17.2.9 client stall (buffered reads of uncached files hang in `fuse_get_req` while O_DIRECT works) seen on a Jetstream2 Slurm node, 2026-11.
