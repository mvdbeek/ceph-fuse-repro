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
| `harness/h1_mechanics.sh` | Fills the FUSE background slots on purpose (OSDs paused, many readers) and probes stat / O_DIRECT / buffered reads |
| `harness/batch.sh`, `harness/batch-targeted.sh` | The long runs and the targeted runs behind results.md |
| `harness/blackhole.sh` | Environmental alternative: drops large packets from one OSD to the node (path-MTU black hole) and probes |
| `harness/report.py` | Regenerates the run tables in results.md from `results/runs.tsv` |
| `scripts/vm.sh`, `docker/qemu/` | Stage B KVM guest |
| `harness/capture.sh` | Capture bundle: client admin sockets, D-state tasks, ps/wchan, fusectl, dmesg, MDS sessions/ops, OSD ops |
| `results/` | One directory per run, plus `runs.tsv` |

Everything runs under rootless docker. State (keyrings, OSD files, logs, control dir) lives in `state/` (git-ignored). Memory caps: mon 320 MB, mgr 640 MB, 3 × OSD 768 MB, MDS 448 MB, 2 × client 1.4 GB (6.5 GB), plus the Stage B guest (3 GB, container cap 3.5 GB); actual use is about 3–5 GB. OSDs at 576 MB were OOM-killed by the burst after a 150 s OSD pause. A client at 1 GB got its ceph-fuse OOM-killed (ceph-fuse alone reaches ~400 MB RSS with the default 200 MB object cacher); `run.py` marks any run with an OOM kill INVALID.

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

## Stage B: the production kernel in a KVM guest

`scripts/vm.sh` boots Rocky 9.8 GenericCloud with elrepo **`kernel-lt-6.1.188-1.el9.elrepo`** (the exact production build, from the elrepo archive) and ceph-fuse 17.2.9 from download.ceph.com. The guest is the "node" client; "head" stays in its container.

- qemu runs in the `cfr-vm` container (`docker/qemu/Dockerfile`, qemu 8.2) on the `cfr` network: rootless docker's network is not routable from the host, but the guest's user-mode (slirp) network gets out through the container. The guest has 3 GB RAM and 4 vCPUs (`VM_MEM_MB`, `VM_CPUS`).
- First boot provisions with cloud-init (packages, kernel, `grubby --set-default`) and reboots into 6.1.188, which takes about 5 minutes. The guest disk is a qcow2 overlay in `state/vm/`.
- `harness/`, `state/ctl`, `results/`, `state/etc` and the guest's ceph log (`state/log-vm`) are shared with the guest over virtiofs (`/share`, `cache=never`).
- `run.py --stage B` / `capture.sh` / `h1_mechanics.sh` (with `CFR_STAGE=B`) reach the guest over ssh. netem for the VM node goes on `cfr-vm`'s `eth0`, the guest's only path out.
- In the guest everything is available as root: fusectl, `dmesg` (hung-task reports, `hung_task_warnings=-1`), `/proc/<pid>/stack`, sysrq-w.

```sh
make vm-up          # needs /dev/kvm usable by uid 1001 (see below); boots, provisions, mounts
make run-b DURATION=3600 FAULTS=mdsrecall LABEL=x WORKLOAD="--workers 4 --hot 4"
harness/batch.sh BA # the long runs in results.md
make vm-down
```

## Access needed on chonk

chonk runs **rootless** docker (container root = host uid 1001). Two root changes were made for this work (both reset at reboot):

```sh
setfacl -m u:babysit:rw /dev/kvm                                   # Stage B
sysctl -w kernel.dmesg_restrict=0 kernel.hung_task_warnings=-1      # host kernel log for Stage A
```

What Stage A can and cannot see:

- **fusectl**: can't be mounted inside a user namespace, but the host's `/sys/fs/fuse/connections/<st_dev>/` files belong to the mounting uid (us), so `run.py` reads `waiting`/`max_background`/`congestion_threshold` from the host every 10 s (`results/<run>/fusectl.tsv`) and at each trigger.
- **dmesg**: readable (host kernel log, shared with everything else on chonk) with `dmesg_restrict=0`.
- **`/proc/<pid>/stack`, sysrq-w**: need CAP_SYS_ADMIN in the initial namespace; not available. `wchan` and `/proc/<pid>/syscall` are recorded instead. Stage B has both.
- The ceph-fuse admin socket has no `dump_ops_in_flight` for clients; `mds_requests` and `objecter_requests` cover in-flight MDS/OSD operations.
