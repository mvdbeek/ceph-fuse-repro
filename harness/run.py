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
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, "state", "ctl")
FAULTS = ("netem", "osdnetem", "msinject", "mdsrecall", "slowosd", "debugclient")
CLIENTS = ("head", "node")


def sh(*cmd, check=True, quiet=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd), r.stderr.strip()))
    return r.stdout.strip()


def dexec(container, *cmd, check=True):
    return sh("docker", "exec", "cfr-" + container, *cmd, check=check)


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
                dexec(c, "tc", "qdisc", "replace", "dev", "eth0", "root", "netem", *spec.split())
        def remove():
            for c in containers:
                dexec(c, "tc", "qdisc", "del", "dev", "eth0", "root", check=False)
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
        "debugclient": client_conf({
            "debug_client": "1/20",
            "debug_objectcacher": "1/20",
            "debug_ms": "0/1",
            # In-memory ring that "log dump" (capture.sh) writes out; sized to
            # stay well inside the client container's memory cap.
            "log_max_recent": "100000",
        }),
    }


def describe_faults(args):
    d = {}
    for f in args.faults:
        d[f] = {
            "netem": args.netem, "osdnetem": args.osd_netem,
            "msinject": "socket_failures=1/%d delay_p=%s max=%ss" % (args.ms_socket_failures, args.ms_delay_p, args.ms_delay_max),
            "mdsrecall": "cache_limit=%d max_caps_per_client=%d" % (args.mds_cache_limit, args.mds_max_caps),
            "slowosd": "dispatch_delay p=%s %ss" % (args.osd_delay_p, args.osd_delay),
            "debugclient": "debug_client=1/20 (in-memory ring)",
        }[f]
    return d


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=3600, help="seconds")
    ap.add_argument("--faults", default="", help="comma list of: " + ",".join(FAULTS))
    ap.add_argument("--label", default="")
    ap.add_argument("--stage", default="A")
    ap.add_argument("--stop-on-repro", action="store_true")
    ap.add_argument("--max-captures", type=int, default=5)
    ap.add_argument("--netem", default="delay 10ms loss 0.2%")
    ap.add_argument("--osd-netem", default="delay 5ms")
    ap.add_argument("--ms-socket-failures", type=int, default=2000)
    ap.add_argument("--ms-delay-p", type=float, default=0.02)
    ap.add_argument("--ms-delay-max", type=float, default=2.0)
    ap.add_argument("--mds-cache-limit", type=int, default=32 << 20)
    ap.add_argument("--mds-max-caps", type=int, default=2000)
    ap.add_argument("--osd-delay-p", type=float, default=0.01)
    ap.add_argument("--osd-delay", type=float, default=0.2)
    ap.add_argument("workload_args", nargs=argparse.REMAINDER,
                    help="after --: extra args for workload.py (e.g. -- --workers 6 --hot 4)")
    args = ap.parse_args()
    args.faults = [f for f in args.faults.split(",") if f]
    for f in args.faults:
        if f not in FAULTS:
            sys.exit("unknown fault %r" % f)
    wargs = [a for a in args.workload_args if a != "--"]

    for c in CLIENTS:
        if subprocess.run(["docker", "exec", "cfr-" + c, "mountpoint", "-q", "/mnt/scratch"]).returncode:
            sys.exit("cfr-%s: /mnt/scratch not mounted; run `make up`" % c)

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

    actions = fault_actions(args)
    applied = []
    try:
        for f in args.faults:
            actions[f][0]()
            applied.append(f)
            log("fault on: %s" % f)
        common = ["--run-id", run_id] + wargs
        for role, container in (("head", "head"), ("node", "node"), ("failcheck", "node")):
            subprocess.run(["docker", "exec", "-d", "cfr-" + container, "sh", "-c",
                            "exec python3 -u /harness/workload.py %s %s > /results/%s/%s.log 2>&1"
                            % (role, " ".join(common), run_id, role)], check=True)
        t0 = time.time()
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
                if trig.get("kind") in ("buffered_hang_direct_ok", "buffered_and_direct_fail", "dstate", "hung_task_dmesg"):
                    repro.append(trig)
                if captures < args.max_captures:
                    captures += 1
                    cdir = os.path.join(rdir, "capture-%03d-%s" % (trig["elapsed"], trig.get("kind")))
                    subprocess.run([os.path.join(ROOT, "harness", "capture.sh"), cdir, json.dumps(trig)])
            if args.stop_on_repro and repro:
                break
            # A role that exits early would make the rest of the run meaningless.
            dead = [role for role, c in (("head", "head"), ("node", "node"), ("failcheck", "node"))
                    if subprocess.run(["docker", "exec", "cfr-" + c, "pgrep", "-f", "workload.py %s " % role],
                                      capture_output=True).returncode]
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
        open(os.path.join(CTL, "stop"), "w").close()
        log("stopping workload")
        deadline = time.time() + 60
        left = True
        while time.time() < deadline:
            left = any(subprocess.run(["docker", "exec", "cfr-" + c, "pgrep", "-f", "workload.py"],
                                      capture_output=True).returncode == 0 for c in CLIENTS)
            if not left:
                break
            time.sleep(2)
        if left:
            log("workload processes still running after stop; capturing and killing")
            subprocess.run([os.path.join(ROOT, "harness", "capture.sh"), os.path.join(rdir, "capture-end-hang"), "end-hang"])
            for c in CLIENTS:
                subprocess.run(["docker", "exec", "cfr-" + c, "pkill", "-9", "-f", "workload.py|md5sum|dd if=|cp -R"])
        for f in reversed(applied):
            actions[f][1]()
            log("fault off: %s" % f)

    stats = {r: read_json(os.path.join(CTL, "stats-%s.json" % r), {}) for r in ("head", "node", "failcheck")}
    blocklist = ceph("osd", "blocklist", "ls", check=False)
    shutil.copy(os.path.join(CTL, "events.jsonl"), rdir)
    kinds = sorted({t.get("kind") for t in repro})
    if aborted:
        outcome = "INVALID: role(s) %s exited early" % ",".join(aborted)
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
    summary = dict(meta, elapsed=elapsed, aborted=aborted, outcome=outcome, triggers=triggers, stats=stats,
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
