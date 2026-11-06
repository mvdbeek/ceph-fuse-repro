# ceph-fuse stall reproducer

Trying to reproduce a ceph-fuse 17.2.9 client stall (buffered reads of uncached files hang in `fuse_get_req` while O_DIRECT works) seen on a Jetstream2 Slurm node, 2026-11.

Results so far: [results.md](results.md).

## What is here

| Path | What |
|---|---|
| `docker/Dockerfile` | Rocky 9 image with ceph 17.2.9 from download.ceph.com (`quay.io/ceph/ceph` has no v17.2.9 tag; it stops at v17.2.8) |
| `scripts/cluster.sh` | `up` / `down [--purge]` / `status` / `mount` / `umount`: 1 mon, 1 mgr, 3 bluestore OSDs (8 GB sparse files), 1 MDS, CephFS `cephfs` with a Manila-style subvolume, and two client containers `cfr-head` and `cfr-node`, each with its own `ceph-fuse` mount (`client.galaxy-head`, `client.galaxy-node`) |
| `harness/workload.py` | Runs inside the client containers: `head`, `node` and `failcheck` roles (see below) |
| `harness/run.py` | Host-side runner: switches faults on, starts the roles, captures state on each trigger, switches faults off, writes `results/<run>/summary.json` and a line in `results/runs.tsv` |
| `harness/capture.sh` | Capture bundle: client admin sockets, D-state tasks, ps/wchan, fusectl, dmesg, MDS sessions/ops, OSD ops |
| `results/` | One directory per run, plus `runs.tsv` |

Everything runs under rootless docker. State (keyrings, OSD files, logs, control dir) lives in `state/` (git-ignored). Memory caps: mon 320 MB, mgr 640 MB, 3 × OSD 576 MB, MDS 448 MB, 2 × client 1.4 GB, 5.94 GB in total; actual use is about 2.5–3.5 GB. A client at 1 GB got its ceph-fuse OOM-killed (ceph-fuse alone reaches ~400 MB RSS with the default 200 MB object cacher); `run.py` marks any run with an OOM kill INVALID.

## Usage

```sh
make build          # once: builds ceph-fuse-repro:17.2.9 (~5 min)
make up             # bootstrap (first time) or restart the cluster, mount both clients
make status

# One run: 1 h, with faults; extra workload.py arguments go in WORKLOAD.
make run DURATION=3600 FAULTS=netem,mdsrecall LABEL=netem-recall WORKLOAD="--workers 4 --hot 4"
# same thing:
python3 harness/run.py --duration 3600 --faults netem,mdsrecall --label netem-recall -- --workers 4 --hot 4

make capture        # manual capture bundle into results/
make down           # stop and remove all containers (keeps state/)
make purge          # down + delete state/
```

`run.py --help` lists the fault parameters. `--stop-on-repro` ends the run at the first failure.

## Workload

Modelled on Galaxy (head) and Pulsar (node) sharing a job directory on CephFS. The two clients coordinate through `state/ctl` on local disk, never through CephFS.

- **head**, `--workers` threads: create job dirs `jobs/<id>/{configs,working,outputs,metadata}` with a 157,770-byte `configs/registry.xml`, 3 config files (10–64 KB) and 5 inputs (10 KB–2 MB, log-uniform). With probability `--rewrite-p` (0.3), two files are rewritten 0–1 s after the job is handed to node, so the rewrite races node's reads. A checker thread reads node's outputs back and verifies them, writes `metadata/galaxy.json`, and deletes jobs older than the last `--keep-jobs` (40).
- **node**, `--workers` threads: claim a job, run Pulsar's prepare-dirs step `cp -R {working,outputs,configs} *.orig` with coreutils `cp` (8.32, as on Rocky 9), read every input buffered or through `mmap`, compare with the `.orig` copy, write 4 outputs (one through a shared `mmap`, so it goes out as page writeback) and a tool stdout file in small appends.
- **hot files** (`--hot N`): head keeps overwriting/appending N files; `--hot-readers` node threads drop their page cache (`POSIX_FADV_DONTNEED`) and re-read them, buffered or via mmap. This aims directly at H3: Fc cap revokes (→ `notify_inval_inode`) while node has readahead in flight.
- **failcheck** (on node), the production health check: every `--probe-interval` (5 s) head writes and fsyncs a fresh 1 MiB probe file; node must `md5sum` it (buffered) within 20 s. On failure it tries `dd iflag=direct`, and triggers `buffered_hang_direct_ok` (the production signature) or `buffered_and_direct_fail`. It also triggers on any task in D state for more than 60 s (`dstate`), on node making no progress for 180 s with work queued (`progress_stall`), and on hung-task lines in `dmesg` (unavailable here, see below).

## Faults (`--faults`, comma separated; recorded in `run.json`)

| Name | What it does |
|---|---|
| `netem` | `tc netem` on both client containers' `eth0` (default `delay 10ms loss 0.2%`, `--netem`; netem jitter reorders packets and collapses TCP throughput, so no jitter by default) |
| `osdnetem` | `tc netem` on the three OSD containers (`--osd-netem`) |
| `msinject` | client `ms_inject_socket_failures` (1 in `--ms-socket-failures`), `ms_inject_delay_type="osd mds"`, `ms_inject_delay_probability`/`max`, set via the admin socket |
| `mdsrecall` | `mds_cache_memory_limit` (default 32 MiB), `mds_max_caps_per_client` (default 2000), `mds_recall_max_caps`, `mds_recall_max_decay_rate`, `mds_cache_trim_threshold`, to force cap recall |
| `slowosd` | `osd_debug_inject_dispatch_delay_probability`/`duration`: the OSD dispatch thread sleeps, so writes (and writeback) stall |
| `debugclient` | `debug_client=1/20`, `debug_objectcacher=1/20` into the in-memory log ring (`log_max_recent=100000`), which `capture.sh` writes out with `log dump` |

## Capture bundle (`results/<run>/capture-<elapsed>-<kind>/`)

Per client: `asok-{status,mds_sessions,mds_requests,objecter_requests,perf_dump,config_diff,dump_cache}`, `ps.txt` (all threads with `wchan`), `dstate.txt` (each D-state task: wchan, syscall, `/proc/<pid>/stack`), `fusectl.txt`, `dmesg.txt`, the tail of the client log after `log dump`. Cluster: `ceph -s`, health, blocklist, MDS `session ls` / `dump_ops_in_flight` / `dump_blocked_ops` / `perf dump`, OSD `dump_ops_in_flight`, `ceph config dump`; plus `events.jsonl`.

## Limits of this environment (Stage A)

chonk runs **rootless** docker (container root = host uid 1001) and this agent has no sudo (no-new-privs). As a result:

- `/sys/fs/fuse/connections` (fusectl) is not mounted where dockerd can see it, and fusectl cannot be mounted from inside a user namespace, so `waiting` / `max_background` / `congestion_threshold` cannot be read. `fusectl.txt` records the failure.
- `dmesg` fails (`dmesg_restrict=1`, needs CAP_SYSLOG in the initial namespace), so hung-task messages can't be watched; `/proc/<pid>/stack` and `/proc/sysrq-trigger` need CAP_SYS_ADMIN in the initial namespace. `wchan` works and is recorded instead.
- The ceph-fuse admin socket has no `dump_ops_in_flight` for clients; `mds_requests` and `objecter_requests` cover in-flight MDS/OSD operations.
- Stage B (KVM VM with the production 6.1.188 elrepo kernel) is blocked: `/dev/kvm` exists, but opening it from a container fails with `EACCES` (uid 1001 is not in the `kvm` group), qemu is not installed, and `sudo` is unavailable. See results.md.

Inside a VM all of the above would be available as real root.
