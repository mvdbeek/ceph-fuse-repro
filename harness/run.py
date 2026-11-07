#!/usr/bin/env python3
"""Run one reproduction attempt against the cluster from scripts/cluster.sh.

  harness/run.py --duration 3600 --faults netem,msinject,mdsrecall,slowosd --hot 4

Applies the chosen faults at runtime, starts the head/node workloads and the
failure check, captures state on every trigger (harness/capture.sh), and at the
end removes the faults and writes results/<run>/summary.json plus a line in
results/runs.tsv.
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, "state", "ctl")
FAULTS = ("netem", "osdnetem", "msinject", "mdsrecall", "slowosd", "debugclient", "fullflap", "octrim")
CLIENTS = ("head", "node")
# Stage B: the "node" client is the KVM guest inside the cfr-vm container.
NODE_VM = False
SSH_GUEST = ["ssh", "-i", "/vm/id_ed25519", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10", "-p", "2222", "root@127.0.0.1"]


def prefix(name, *cmd):
    """argv that runs cmd in the container cfr-<name>, or in the guest for node in Stage B."""
    if name == "node" and NODE_VM:
        return ["docker", "exec", "cfr-vm"] + SSH_GUEST + [shlex.join(cmd)]
    return ["docker", "exec", "cfr-" + name] + list(cmd)


def netns(name):
    """Container whose eth0 carries name's traffic (for tc netem)."""
    return "vm" if name == "node" and NODE_VM else name


def start_detached(name, script):
    if name == "node" and NODE_VM:
        # all fds redirected, or ssh waits for the detached process
        sh(*prefix(name, "sh", "-c", 'setsid -f sh -c "$1" </dev/null >/dev/null 2>&1', "sh", script))
    else:
        subprocess.run(["docker", "exec", "-d", "cfr-" + name, "sh", "-c", script], check=True)


def running(name, pattern):
    return subprocess.run(prefix(name, "pgrep", "-f", pattern), capture_output=True).returncode == 0


def fusectl(name):
    """waiting/max_background/congestion_threshold of name's /mnt/scratch FUSE connection."""
    keys = ("waiting", "max_background", "congestion_threshold")
    try:
        dev = sh(*prefix(name, "stat", "-c", "%d", "/mnt/scratch"))
        if name == "node" and NODE_VM:
            out = sh(*prefix(name, "sh", "-c", "cd /sys/fs/fuse/connections/%s && cat %s" % (dev, " ".join(keys))))
            return dict(zip(keys, out.split()))
        # Stage A: the host's fusectl; the files belong to the mounting uid (rootless docker = us).
        d = "/sys/fs/fuse/connections/%s" % dev
        return {k: open(os.path.join(d, k)).read().strip() for k in keys}
    except (OSError, RuntimeError) as e:
        return {"error": str(e)[:200]}


def sh(*cmd, check=True, quiet=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd), r.stderr.strip()))
    return r.stdout.strip()


def dexec(container, *cmd, check=True):
    return sh(*prefix(container, *cmd), check=check)


def asok(client, *cmd):
    return dexec(client, "ceph", "--admin-daemon", "/var/run/ceph/ceph-client.galaxy-%s.asok" % client, *cmd)


def ceph(*cmd, check=True):
    return dexec("mon", "ceph", *cmd, check=check)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# Each fault: (apply, remove). Parameters come from args.
def fault_actions(args):
    def tc(containers, spec):
        def apply():
            for c in containers:
                sh("docker", "exec", "cfr-" + netns(c), "tc", "qdisc", "replace", "dev", "eth0", "root", "netem", *spec.split())
        def remove():
            for c in containers:
                sh("docker", "exec", "cfr-" + netns(c), "tc", "qdisc", "del", "dev", "eth0", "root", check=False)
        return apply, remove

    def client_conf(settings):
        def apply():
            for c in CLIENTS:
                for k, v in settings.items():
                    asok(c, "config", "set", k, v)
        def remove():
            for c in CLIENTS:
                for k in settings:
                    try:
                        asok(c, "config", "unset", k)
                    except RuntimeError:
                        pass
        return apply, remove

    def cluster_conf(who, settings):
        def apply():
            for k, v in settings.items():
                ceph("config", "set", who, k, v)
        def remove():
            for k in settings:
                ceph("config", "rm", who, k, check=False)
        return apply, remove

    return {
        "netem": tc(CLIENTS, args.netem),
        "osdnetem": tc(("osd0", "osd1", "osd2"), args.osd_netem),
        "msinject": client_conf({
            "ms_inject_socket_failures": str(args.ms_socket_failures),
            "ms_inject_delay_type": "osd mds",
            "ms_inject_delay_probability": str(args.ms_delay_p),
            "ms_inject_delay_max": str(args.ms_delay_max),
        }),
        "mdsrecall": cluster_conf("mds", {
            "mds_cache_memory_limit": str(args.mds_cache_limit),
            "mds_max_caps_per_client": str(args.mds_max_caps),
            "mds_recall_max_caps": "500",
            "mds_recall_max_decay_rate": "1.0",
            "mds_cache_trim_threshold": "16384",
        }),
        "slowosd": cluster_conf("osd", {
            "osd_debug_inject_dispatch_delay_probability": str(args.osd_delay_p),
            "osd_debug_inject_dispatch_delay_duration": str(args.osd_delay),
        }),
        # Tiny ObjectCacher: trim() evicts clean/zero buffers on nearly every op,
        # widening the discard-then-trim window for in-flight reads.
        "octrim": client_conf({"client_oc_size": str(args.oc_size)}),
        "debugclient": client_conf({
            # Straight to the log file: "log dump" of the in-memory ring crashed
            # ceph-fuse 17.2.9 (see capture.sh).
            "debug_client": "10/10",
            "debug_objectcacher": "10/10",
            "debug_ms": "0/0",
        }),
    }


DEFAULT_RATIOS = (("full", "0.95"), ("backfillfull", "0.90"), ("nearfull", "0.85"))


def full_flapper(args, stop, tsv):
    """Make the cluster briefly FULL: ratios just below current OSD use for
    args.flap_seconds, every args.flap_interval seconds. Seen once by accident:
    6 s of OSD_FULL, then a 15 min fn_anonymous invalidation stall."""
    t0 = time.time()
    while not stop.wait(args.flap_interval):
        util = max(n["utilization"] for n in json.loads(ceph("osd", "df", "-f", "json"))["nodes"]) / 100
        low = max(util - 0.01, 0.03)
        for name, v in (("nearfull", low - 0.02), ("backfillfull", low - 0.01), ("full", low)):
            ceph("osd", "set-%s-ratio" % name, "%.4f" % v)
        a = time.time()
        stop.wait(args.flap_seconds)
        for name, v in DEFAULT_RATIOS:
            ceph("osd", "set-%s-ratio" % name, v)
        tsv.write("%d\t%d\t%.4f\n" % (a - t0, time.time() - t0, util))
        tsv.flush()


def describe_faults(args):
    d = {}
    for f in args.faults:
        d[f] = {
            "netem": args.netem, "osdnetem": args.osd_netem,
            "msinject": "socket_failures=1/%d delay_p=%s max=%ss" % (args.ms_socket_failures, args.ms_delay_p, args.ms_delay_max),
            "mdsrecall": "cache_limit=%d max_caps_per_client=%d" % (args.mds_cache_limit, args.mds_max_caps),
            "slowosd": "dispatch_delay p=%s %ss" % (args.osd_delay_p, args.osd_delay),
            "debugclient": "debug_client=10, debug_objectcacher=10 (to the log file)",
            "octrim": "client_oc_size=%d" % args.oc_size,
            "fullflap": "cluster FULL for %ss every %ss (ratios below current use)" % (args.flap_seconds, args.flap_interval),
        }[f]
    return d


def host_mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 1 << 30


def running_in_container_node():
    return subprocess.run(["docker", "exec", "cfr-node", "pgrep", "-f", "workload.py"], capture_output=True).returncode == 0


def container_state(name):
    """(running, oom_killed) of cfr-<name> according to docker."""
    out = sh("docker", "inspect", "-f", "{{.State.Running}} {{.State.OOMKilled}}", "cfr-" + name, check=False).split()
    return (out[:1] == ["true"], out[1:2] == ["true"])


def oom_kills(name):
    if not container_state(name)[0]:
        return 1 << 20 if container_state(name)[1] else 0  # dead from an OOM kill: memory.events is gone with it
    if name == "node" and NODE_VM:
        # No cgroup inside the guest's view of itself to speak of: count kernel OOM kills.
        out = sh(*prefix(name, "sh", "-c", "dmesg | grep -c 'Killed process' || true"), check=False)
        return int(out or 0)
    ev = dexec(name, "cat", "/sys/fs/cgroup/memory.events", check=False)
    return int(dict(l.split() for l in ev.splitlines()).get("oom_kill", 0)) if ev else 0


def raw_used():
    return json.loads(ceph("df", "-f", "json"))["stats"]["total_used_raw_ratio"]


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


stop_requested = []


def request_stop(signum, frame):
    # Let the main loop end the run, so cleanup and summary.json always happen.
    stop_requested.append(signum)


def main():
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=3600, help="seconds")
    ap.add_argument("--faults", default="", help="comma list of: " + ",".join(FAULTS))
    ap.add_argument("--label", default="")
    ap.add_argument("--stage", default="A", choices=["A", "B"], help="B: node is the KVM guest (scripts/vm.sh)")
    ap.add_argument("--stop-on-repro", action="store_true")
    ap.add_argument("--max-captures", type=int, default=5)
    ap.add_argument("--min-host-mem", type=int, default=600, help="abort if host MemAvailable (MB) drops below")
    ap.add_argument("--netem", default="delay 10ms loss 0.2%")
    ap.add_argument("--osd-netem", default="delay 5ms")
    ap.add_argument("--ms-socket-failures", type=int, default=2000)
    ap.add_argument("--ms-delay-p", type=float, default=0.02)
    ap.add_argument("--ms-delay-max", type=float, default=2.0)
    ap.add_argument("--mds-cache-limit", type=int, default=32 << 20)
    ap.add_argument("--mds-max-caps", type=int, default=2000)
    ap.add_argument("--osd-delay-p", type=float, default=0.01)
    ap.add_argument("--osd-delay", type=float, default=0.2)
    ap.add_argument("--oc-size", type=int, default=1 << 20)
    ap.add_argument("--flap-seconds", type=float, default=6)
    ap.add_argument("--flap-interval", type=float, default=600)
    ap.add_argument("workload_args", nargs=argparse.REMAINDER,
                    help="after --: extra args for workload.py (e.g. -- --workers 6 --hot 4)")
    args = ap.parse_args()
    args.faults = [f for f in args.faults.split(",") if f]
    for f in args.faults:
        if f not in FAULTS:
            sys.exit("unknown fault %r" % f)
    wargs = [a for a in args.workload_args if a != "--"]
    global NODE_VM
    NODE_VM = args.stage == "B"
    os.environ["CFR_STAGE"] = args.stage  # for capture.sh

    for c in CLIENTS:
        if subprocess.run(prefix(c, "mountpoint", "-q", "/mnt/scratch")).returncode:
            sys.exit("%s: /mnt/scratch not mounted; run `make up` (and `make vm-up vm-mount` for stage B)" % c)
    down = [c for c in ("mon", "mgr", "mds", "osd0", "osd1", "osd2") if not container_state(c)[0]]
    if down:
        sys.exit("cluster container(s) not running: %s" % ",".join(down))
    if raw_used() > 0.75:
        sys.exit("cluster raw use %.0f%% > 75%%; clean up first" % (raw_used() * 100))
    if NODE_VM and running_in_container_node():
        sys.exit("stage B: stop the workload in cfr-node first")

    run_id = time.strftime("%Y%m%d-%H%M%S") + ("-" + args.label if args.label else "")
    rdir = os.path.join(ROOT, "results", run_id)
    os.makedirs(rdir)
    meta = {
        "run_id": run_id, "stage": args.stage,
        "kernel": dexec("node", "uname", "-r"),
        "ceph_fuse": dexec("node", "ceph-fuse", "--version"),
        "cluster": ceph("version"),
        "faults": describe_faults(args),
        "workload_args": wargs,
        "start": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "planned_duration": args.duration,
    }
    with open(os.path.join(rdir, "run.json"), "w") as f:
        json.dump(meta, f, indent=2)
    log("run %s: faults=%s" % (run_id, ",".join(args.faults) or "none"))

    # Fresh control dir.
    if os.path.isdir(CTL):
        for n in os.listdir(CTL):
            p = os.path.join(CTL, n)
            shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
    os.makedirs(os.path.join(CTL, "trigger"), exist_ok=True)

    containers = ("mon", "mgr", "mds", "osd0", "osd1", "osd2") + CLIENTS
    oom_before = {c: oom_kills(c) for c in containers}
    fusectl_tsv = open(os.path.join(rdir, "fusectl.tsv"), "w")
    fusectl_tsv.write("elapsed\tclient\twaiting\tmax_background\tcongestion_threshold\n")
    actions = fault_actions(args)
    flap_stop = threading.Event()
    if "fullflap" in args.faults:
        flap_tsv = open(os.path.join(rdir, "fullflap.tsv"), "w")
        flap_tsv.write("start_elapsed\tend_elapsed\tmax_osd_util\n")
        actions["fullflap"] = (
            lambda: threading.Thread(target=full_flapper, args=(args, flap_stop, flap_tsv), daemon=True).start(),
            lambda: (flap_stop.set(), time.sleep(args.flap_seconds + 2),
                     [ceph("osd", "set-%s-ratio" % n, v) for n, v in DEFAULT_RATIOS]))
    applied = []
    try:
        for f in args.faults:
            actions[f][0]()
            applied.append(f)
            log("fault on: %s" % f)
        # Delete the previous run's trees now, before the clock starts. (Moving them
        # aside and deleting in the background let them pile up until the OSDs filled.)
        t_clean = time.time()
        # 8 parallel rm (one rm through ceph-fuse took >30 min for a run's jobs);
        # whatever is left after 15 min is picked up by the next run's cleanup.
        dexec("head", "timeout", "900", "sh", "-c",
              "for d in /mnt/scratch/trash /mnt/scratch/jobs /mnt/scratch/probe /mnt/scratch/hot; do "
              "[ -d $d ] && (cd $d && ls | xargs -r -P 8 -n 20 rm -rf); rmdir $d 2>/dev/null; done; true", check=False)
        log("cleanup took %ds" % (time.time() - t_clean))
        common = ["--run-id", run_id] + wargs
        for role, container in (("head", "head"), ("node", "node"), ("failcheck", "node")):
            start_detached(container, "exec python3 -u /harness/workload.py %s %s > /results/%s/%s.log 2>&1"
                           % (role, " ".join(common), run_id, role))
        t0 = time.time()
        last_fusectl = 0
        capture_procs = []
        captures = 0
        repro = []
        triggers = []
        last_status = 0
        aborted = None
        while time.time() - t0 < args.duration:
            for name in sorted(os.listdir(os.path.join(CTL, "trigger"))):
                if name.startswith("."):
                    continue
                tpath = os.path.join(CTL, "trigger", name)
                trig = read_json(tpath, {})
                os.unlink(tpath)
                trig["elapsed"] = round(time.time() - t0)
                triggers.append(trig)
                log("TRIGGER %s after %ds: %s" % (trig.get("kind"), trig["elapsed"], json.dumps(trig)[:400]))
                if trig.get("kind") in ("buffered_hang_direct_ok", "buffered_and_direct_fail", "dstate", "hung_task_dmesg", "unkillable_probe"):
                    repro.append(trig)
                if captures < args.max_captures:
                    captures += 1
                    cdir = os.path.join(rdir, "capture-%03d-%s" % (trig["elapsed"], trig.get("kind")))
                    trig["fusectl_host"] = {c: fusectl(c) for c in CLIENTS}
                    # In the background: a Stage B capture takes minutes over ssh,
                    # and the loop must keep sampling and checking meanwhile.
                    capture_procs.append(subprocess.Popen(
                        [os.path.join(ROOT, "harness", "capture.sh"), cdir, json.dumps(trig)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            if args.stop_on_repro and repro:
                break
            if stop_requested:
                log("stop requested (signal %d)" % stop_requested[0])
                aborted = ["stopped by hand"]
                break
            if time.time() - last_fusectl >= 10:
                last_fusectl = time.time()
                for c in CLIENTS:
                    v = fusectl(c)
                    fusectl_tsv.write("%d\t%s\t%s\t%s\t%s\n" % (time.time() - t0, c, v.get("waiting"), v.get("max_background"),
                                                               v.get("congestion_threshold", v.get("error"))))
                fusectl_tsv.flush()
            # chonk is shared: back off rather than push the host into OOM.
            if host_mem_available_mb() < args.min_host_mem:
                log("host MemAvailable %d MB < %d MB; aborting run" % (host_mem_available_mb(), args.min_host_mem))
                aborted = ["host-memory-pressure"]
                break
            if time.time() - last_fusectl < 3 and raw_used() > 0.85:
                log("cluster raw use above 85%; aborting run before it fills")
                aborted = ["cluster-filling"]
                break
            # A cluster daemon that died (e.g. OOM-killed) makes the rest of the run meaningless.
            down = [c for c in ("mon", "mgr", "mds", "osd0", "osd1", "osd2") if not container_state(c)[0]]
            if down:
                log("cluster container(s) not running: %s; aborting run" % ",".join(down))
                aborted = ["daemon-down:" + ",".join(down)]
                break
            # A role that exits early would make the rest of the run meaningless.
            dead = [role for role, c in (("head", "head"), ("node", "node"), ("failcheck", "node"))
                    if not running(c, "workload.py %s " % role)]
            if dead:
                log("workload role(s) exited early: %s; aborting run" % ",".join(dead))
                aborted = dead
                break
            if time.time() - last_status > 300:
                last_status = time.time()
                h, n = read_json(os.path.join(CTL, "stats-head.json"), {}), read_json(os.path.join(CTL, "stats-node.json"), {})
                fc = read_json(os.path.join(CTL, "stats-failcheck.json"), {})
                log("t=%ds jobs created=%s done=%s checked=%s hot w/r=%s/%s probes ok=%s fail=%s" % (
                    time.time() - t0, h.get("head_jobs_created"), n.get("node_jobs_done"), h.get("head_jobs_checked"),
                    h.get("head_hot_writes"), n.get("node_hot_reads"), fc.get("probe_ok"), fc.get("probe_buffered_fail")))
            time.sleep(2)
        elapsed = round(time.time() - t0)
    finally:
        for p in locals().get("capture_procs", []):
            p.wait()
        open(os.path.join(CTL, "stop"), "w").close()
        log("stopping workload")
        deadline = time.time() + 60
        left = True
        while time.time() < deadline:
            left = any(running(c, "workload.py") for c in CLIENTS)
            if not left:
                break
            time.sleep(2)
        if left:
            log("workload processes still running after stop; capturing and killing")
            subprocess.run([os.path.join(ROOT, "harness", "capture.sh"), os.path.join(rdir, "capture-end-hang"), "end-hang"])
            for c in CLIENTS:
                subprocess.run(prefix(c, "pkill", "-9", "-f", "workload.py|md5sum|dd if=|cp -R"))
        for f in reversed(applied):
            actions[f][1]()
            log("fault off: %s" % f)

    # H1 check: with the workload gone and faults off, every FUSE request should
    # have been answered. Anything still "waiting" was never answered.
    time.sleep(30)
    idle_waiting = {c: fusectl(c) for c in CLIENTS}
    leaked = {c: int(v["waiting"]) for c, v in idle_waiting.items() if v.get("waiting", "0").isdigit() and int(v["waiting"]) > 0}
    if leaked:
        log("idle FUSE requests not answered: %s; capturing" % leaked)
        subprocess.run([os.path.join(ROOT, "harness", "capture.sh"), os.path.join(rdir, "capture-idle-leak"), "idle-leak %s" % leaked])
    fusectl_tsv.close()
    ooms = {}
    for c in containers:
        n = oom_kills(c)
        if n > oom_before.get(c, 0):
            ooms[c] = n - oom_before.get(c, 0)
    mem_peak_mb = {}
    for c in containers + (("vm",) if NODE_VM else ()):
        pk = sh("docker", "exec", "cfr-" + c, "cat", "/sys/fs/cgroup/memory.peak", check=False)
        mem_peak_mb[c] = int(pk) >> 20 if pk.isdigit() else None
    stats = {r: read_json(os.path.join(CTL, "stats-%s.json" % r), {}) for r in ("head", "node", "failcheck")}
    blocklist = ceph("osd", "blocklist", "ls", check=False)
    shutil.copy(os.path.join(CTL, "events.jsonl"), rdir)
    kinds = sorted({t.get("kind") for t in repro})
    if ooms:
        outcome = "INVALID: OOM kill in %s" % ",".join(sorted(ooms))
    elif aborted:
        outcome = "INVALID: aborted (%s)" % ",".join(aborted)
    elif leaked:
        outcome = "LEAK: FUSE requests unanswered after the workload stopped: %s" % leaked
    elif any(t.get("kind") == "buffered_hang_direct_ok" for t in repro):
        outcome = "REPRODUCED: buffered read hung, O_DIRECT ok"
    elif repro:
        outcome = "stall: " + ",".join(kinds)
    elif triggers:
        outcome = "no repro (other triggers: %s)" % ",".join(sorted({t.get("kind") for t in triggers}))
    else:
        outcome = "no repro"
    if left:
        outcome += "; workload hung at stop"
    summary = dict(meta, elapsed=elapsed, aborted=aborted, oom_kills=ooms, mem_peak_mb=mem_peak_mb, idle_fusectl=idle_waiting, outcome=outcome, triggers=triggers, stats=stats,
                   blocklist=blocklist, workload_left_running=left,
                   first_repro_after=repro[0]["elapsed"] if repro else None)
    with open(os.path.join(rdir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    line = "\t".join(str(x) for x in (run_id, args.stage, meta["kernel"], meta["ceph_fuse"].split()[2],
                                      ",".join(args.faults) or "none", " ".join(wargs), elapsed, outcome))
    tsv = os.path.join(ROOT, "results", "runs.tsv")
    new = not os.path.exists(tsv)
    with open(tsv, "a") as f:
        if new:
            f.write("run_id\tstage\tkernel\tceph\tfaults\tworkload_args\tduration_s\toutcome\n")
        f.write(line + "\n")
    log("done: %s after %ds" % (outcome, elapsed))
    log("jobs done=%s probes ok=%s, buffered fails=%s" % (
        stats["node"].get("node_jobs_done"), stats["failcheck"].get("probe_ok"), stats["failcheck"].get("probe_buffered_fail")))


if __name__ == "__main__":
    main()
