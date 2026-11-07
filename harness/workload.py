#!/usr/bin/env python3
"""Galaxy/Pulsar-like I/O on a CephFS mount shared by two ceph-fuse clients.

Runs inside the client containers. Roles:

  head       creates job dirs (many 10 KB-2 MB files, a 157 KB registry.xml),
             rewrites some of them while "node" may be reading, reads node's
             outputs back, deletes old jobs, writes a fresh probe file every
             few seconds and (optionally) keeps rewriting a set of hot files.
  node       claims job dirs and runs Pulsar's prepare-dirs step
             (`cp -R dir dir.orig`), buffered-reads and mmaps the inputs,
             writes outputs (buffered and through a shared mmap); optionally
             re-reads the hot files with the page cache dropped.
  failcheck  (on node) reads each fresh probe file buffered with a 20 s
             timeout and, if that fails, with O_DIRECT; watches for tasks in D
             state for more than 60 s, for workload progress stalls and for
             hung-task messages in dmesg; drops a trigger file for the host
             runner to capture state.

The two clients coordinate through /ctl, a host directory on local disk, never
through CephFS itself.
"""
import argparse
import faulthandler
import hashlib
import json
import mmap
import os
import random
import shutil
import subprocess
import sys
import threading
import time

MNT = "/mnt/scratch"
CTL = "/ctl"
MiB = 1 << 20

stop_event = threading.Event()
counters = {}
counters_lock = threading.Lock()


def bump(name, n=1):
    with counters_lock:
        counters[name] = counters.get(name, 0) + n
        counters["last_" + name] = time.time()


def event(kind, **kw):
    rec = {"t": time.time(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": os.uname().nodename, "kind": kind}
    rec.update(kw)
    line = json.dumps(rec) + "\n"
    with open(os.path.join(CTL, "events.jsonl"), "a") as f:
        f.write(line)
    print(line, end="", flush=True)


def trigger(kind, **kw):
    """Ask the host runner to capture state."""
    d = os.path.join(CTL, "trigger")
    os.makedirs(d, exist_ok=True)
    name = "%d-%s.json" % (time.time() * 1000, kind)
    tmp = os.path.join(d, "." + name)
    with open(tmp, "w") as f:
        json.dump(dict(kind=kind, t=time.time(), **kw), f)
    os.rename(tmp, os.path.join(d, name))
    event("trigger", trigger=kind, **kw)


def stopping():
    return stop_event.is_set() or os.path.exists(os.path.join(CTL, "stop"))


def stats_writer(role):
    path = os.path.join(CTL, "stats-%s.json" % role)
    while not stop_event.is_set():
        with counters_lock:
            snap = dict(counters, t=time.time())
        with open(path + ".tmp", "w") as f:
            json.dump(snap, f)
        os.rename(path + ".tmp", path)
        if stopping():
            stop_event.set()
        stop_event.wait(5)


def md5_bytes(b):
    return hashlib.md5(b).hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(256 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def rand_bytes(n, rng):
    # Compressible-ish but unique content; cheap to generate.
    seed = rng.getrandbits(64).to_bytes(8, "little")
    block = hashlib.sha512(seed).digest() * 64  # 4 KiB
    reps = n // len(block) + 1
    return (block * reps)[:n - 16] + seed + rng.getrandbits(64).to_bytes(8, "little")


def write_file(path, data, fsync=False):
    with open(path, "wb") as f:
        f.write(data)
        if fsync:
            f.flush()
            os.fsync(f.fileno())


def atomic_token(dirname, name, payload):
    d = os.path.join(CTL, dirname)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "." + name)
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, os.path.join(d, name))


def sizes(rng, n, lo=10 * 1024, hi=2 * MiB):
    # Log-uniform between lo and hi: mostly small, some medium.
    import math
    return [int(math.exp(rng.uniform(math.log(lo), math.log(hi)))) for _ in range(n)]


# ---------------------------------------------------------------- head


def head_producer(wid, args):
    rng = random.Random(os.getpid() * 1000 + wid)
    seq = 0
    while not stopping():
        seq += 1
        job = "%s-w%d-%06d" % (args.run_id, wid, seq)
        jd = os.path.join(MNT, "jobs", job)
        manifest = {}
        rewrites = {}
        try:
            for sub in ("configs", "working", "outputs", "metadata"):
                os.makedirs(os.path.join(jd, sub), exist_ok=True)
            files = {"configs/registry.xml": 157770}
            for i, s in enumerate(sizes(rng, 3, 10 * 1024, 64 * 1024)):
                files["configs/conf%d.xml" % i] = s
            for i, s in enumerate(sizes(rng, 5)):
                files["working/input%d.dat" % i] = s
            for rel, size in files.items():
                data = rand_bytes(size, rng)
                write_file(os.path.join(jd, rel), data)
                manifest[rel] = [md5_bytes(data)]
                bump("head_bytes_written", size)
            # Some files will be rewritten after the node was told about the job.
            if rng.random() < args.rewrite_p:
                for rel in rng.sample(sorted(files), 2):
                    data = rand_bytes(files[rel] + rng.randint(-4096, 4096), rng)
                    rewrites[rel] = data
                    manifest[rel].append(md5_bytes(data))
            write_file(os.path.join(jd, "command.sh"), b"#!/bin/sh\n# prepare-dirs, then tool\n")
            atomic_token("ready", job, {"job": job, "manifest": manifest, "rewritten": sorted(rewrites), "t": time.time()})
            bump("head_jobs_created")
            if rewrites:
                time.sleep(rng.uniform(0, args.rewrite_delay))
                for rel, data in rewrites.items():
                    write_file(os.path.join(jd, rel), data)
                bump("head_jobs_rewritten")
        except OSError as e:
            bump("head_errors")
            event("head_error", job=job, error=repr(e))
        # Throttle: do not run far ahead of the node.
        while not stopping() and len(os.listdir(os.path.join(CTL, "ready"))) > args.max_backlog:
            time.sleep(0.2)


def head_checker(args):
    rng = random.Random(os.getpid())
    done_dir = os.path.join(CTL, "done")
    os.makedirs(done_dir, exist_ok=True)
    checked = []
    while not stopping():
        names = sorted(n for n in os.listdir(done_dir) if not n.startswith("."))
        if not names:
            time.sleep(0.5)
            continue
        # The checker is slower than the node; skip (and delete) the oldest
        # jobs beyond a backlog of 50 so the data on the cluster stays bounded.
        while len(names) > 50:
            name = names.pop(0)
            os.rename(os.path.join(done_dir, name), os.path.join(CTL, "checked", name))
            shutil.rmtree(os.path.join(MNT, "jobs", name), ignore_errors=True)
            bump("head_checks_skipped")
            bump("head_jobs_deleted")
        for name in names:
            path = os.path.join(done_dir, name)
            with open(path) as f:
                tok = json.load(f)
            os.rename(path, os.path.join(CTL, "checked", name))
            jd = os.path.join(MNT, "jobs", tok["job"])
            try:
                for rel, md5 in tok["outputs"].items():
                    got = md5_file(os.path.join(jd, rel))
                    if got != md5:
                        bump("head_output_mismatch")
                        event("output_mismatch", job=tok["job"], file=rel, want=md5, got=got)
                    else:
                        bump("head_outputs_verified")
                # Galaxy sets metadata / rewrites a config after the job.
                write_file(os.path.join(jd, "metadata", "galaxy.json"), rand_bytes(rng.randint(2000, 20000), rng))
                checked.append(jd)
                bump("head_jobs_checked")
            except OSError as e:
                bump("head_errors")
                event("head_error", job=tok["job"], error=repr(e))
            while len(checked) > args.keep_jobs:
                old = checked.pop(0)
                shutil.rmtree(old, ignore_errors=True)
                bump("head_jobs_deleted")


def head_probe_writer(args):
    rng = random.Random(os.getpid() + 7)
    pd = os.path.join(MNT, "probe")
    os.makedirs(pd, exist_ok=True)
    seq = 0
    written = []
    while not stopping():
        seq += 1
        path = os.path.join(pd, "%s-p%06d.bin" % (args.run_id, seq))
        try:
            data = rand_bytes(MiB, rng)
            write_file(path, data, fsync=True)
            atomic_token("probe", "%06d" % seq, {"seq": seq, "path": path, "md5": md5_bytes(data), "t": time.time()})
            bump("head_probes_written")
            written.append(path)
            if len(written) > 100:
                os.unlink(written.pop(0))
        except OSError as e:
            bump("head_errors")
            event("head_error", where="probe", error=repr(e))
        stop_event.wait(args.probe_interval)


def head_hot_writer(args):
    rng = random.Random(os.getpid() + 11)
    # Per-run directory; files appear by rename, fully written, so a node
    # reader never maps a file that later shrinks (that would be a SIGBUS).
    hd = os.path.join(MNT, "hot", args.run_id)
    os.makedirs(hd, exist_ok=True)
    paths = [os.path.join(hd, "hot%02d.dat" % i) for i in range(args.hot)]
    for p in paths:
        write_file(p + ".tmp", rand_bytes(256 * 1024, rng))
        os.rename(p + ".tmp", p)
    while not stopping():
        p = rng.choice(paths)
        try:
            if args.hot_truncate and rng.random() < 0.5:
                # Truncating rewrite (like `cp`/`>` over a file node is reading):
                # on node this is a truncate_seq bump -> _invalidate_inode_cache ->
                # ObjectCacher discard of in-flight readahead + kernel invalidate.
                write_file(p, rand_bytes(rng.randint(16 * 1024, 2 * MiB), rng))
                bump("head_hot_truncates")
                bump("head_hot_writes")
                time.sleep(rng.uniform(0.02, args.hot_interval))
                continue
            # Overwrite in place or append, never shrink: node mmaps these
            # files, and a page beyond a truncated EOF would be a SIGBUS.
            fd = os.open(p, os.O_WRONLY)
            try:
                size = os.fstat(fd).st_size
                if rng.random() < 0.7 or size > 4 * MiB:
                    os.pwrite(fd, rand_bytes(rng.randint(4096, min(size, MiB)), rng), 0)
                else:
                    os.pwrite(fd, rand_bytes(rng.randint(1024, 64 * 1024), rng), size)
            finally:
                os.close(fd)
            bump("head_hot_writes")
        except OSError as e:
            bump("head_errors")
            event("head_error", where="hot", error=repr(e))
        time.sleep(rng.uniform(0.02, args.hot_interval))


def run_head(args):
    for d in ("ready", "claimed", "done", "checked", "probe"):
        os.makedirs(os.path.join(CTL, d), exist_ok=True)
    threads = [threading.Thread(target=head_producer, args=(i, args), daemon=True) for i in range(args.workers)]
    threads.append(threading.Thread(target=head_checker, args=(args,), daemon=True))
    threads.append(threading.Thread(target=head_probe_writer, args=(args,), daemon=True))
    if args.hot:
        threads.append(threading.Thread(target=head_hot_writer, args=(args,), daemon=True))
    return threads


# ---------------------------------------------------------------- node


def node_job(job, tok, args, rng):
    jd = os.path.join(MNT, "jobs", job)
    rewritten = set(tok["rewritten"])
    # Pulsar's prepare-dirs step (PREPARE_DIRS_TEMPLATE), with coreutils cp.
    for sub in ("working", "outputs", "configs"):
        src, dst = os.path.join(jd, sub), os.path.join(jd, sub + ".orig")
        t0 = time.time()
        r = subprocess.run(["cp", "-R", src, dst], capture_output=True)
        dt = time.time() - t0
        if r.returncode:
            bump("node_cp_errors")
            event("cp_error", job=job, dir=sub, rc=r.returncode, stderr=r.stderr.decode()[-500:])
        if dt > 10:
            event("slow_cp", job=job, dir=sub, seconds=round(dt, 1))
        bump("node_cp")
    # The "tool": read every input buffered; mmap some.
    for rel, md5s in tok["manifest"].items():
        path = os.path.join(jd, rel)
        # mmap only files head never rewrites (a shrinking rewrite would SIGBUS).
        if rel not in rewritten and rng.random() < args.mmap_p:
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size == 0:
                    continue
                with mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ) as m:
                    got = md5_bytes(m[:])
            bump("node_mmap_reads")
        else:
            got = md5_file(path)
            bump("node_buffered_reads")
        if got not in md5s:
            if rel in rewritten:
                bump("node_torn_reads")  # raced with head's rewrite: expected
            else:
                bump("node_read_mismatch")
                event("read_mismatch", job=job, file=rel, want=md5s, got=got)
        # The copy made by cp must match what we just read for unrewritten files.
        if rel not in rewritten:
            orig = path.replace("/configs/", "/configs.orig/").replace("/working/", "/working.orig/")
            if md5_file(orig) != got:
                bump("node_cp_mismatch")
                event("cp_mismatch", job=job, file=rel)
    # Outputs: buffered writes, a shared-mmap write (page writeback), a log
    # appended in small pieces like tool stdout.
    outputs = {}
    for i, size in enumerate(sizes(rng, 4)):
        rel = "outputs/out%d.dat" % i
        data = rand_bytes(size, rng)
        path = os.path.join(jd, rel)
        if i == 0:
            with open(path, "w+b") as f:
                f.truncate(size)
                with mmap.mmap(f.fileno(), size) as m:
                    m[:] = data
                    if rng.random() < 0.5:
                        m.flush()
            bump("node_mmap_writes")
        else:
            write_file(path, data)
        outputs[rel] = md5_bytes(data)
        bump("node_bytes_written", size)
    log = b""
    with open(os.path.join(jd, "outputs", "tool_stdout"), "wb") as f:
        for _ in range(rng.randint(5, 50)):
            chunk = rand_bytes(rng.randint(50, 4000), rng)
            f.write(chunk)
            f.flush()
            log += chunk
    outputs["outputs/tool_stdout"] = md5_bytes(log)
    write_file(os.path.join(jd, "metadata", "__instrument_core_epoch_start"), b"%d\n" % time.time())
    return outputs


def node_worker(wid, args):
    rng = random.Random(os.getpid() * 1000 + wid)
    ready = os.path.join(CTL, "ready")
    while not stopping():
        names = [n for n in os.listdir(ready) if not n.startswith(".")]
        if not names:
            time.sleep(0.2)
            continue
        job = rng.choice(names)
        claimed = os.path.join(CTL, "claimed", job)
        try:
            os.rename(os.path.join(ready, job), claimed)
        except FileNotFoundError:
            continue  # another worker got it
        with open(claimed) as f:
            tok = json.load(f)
        t0 = time.time()
        try:
            outputs = node_job(job, tok, args, rng)
        except OSError as e:
            bump("node_errors")
            event("node_error", job=job, error=repr(e))
            continue
        atomic_token("done", job, {"job": job, "outputs": outputs, "seconds": time.time() - t0})
        os.unlink(claimed)
        bump("node_jobs_done")


HOT_MMAP_READ = """
import mmap, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
if os.fstat(fd).st_size:
    with mmap.mmap(fd, 0, prot=mmap.PROT_READ) as m:
        m[:]
"""


def node_hot_reader(wid, args):
    rng = random.Random(os.getpid() * 1000 + 500 + wid)
    hd = os.path.join(MNT, "hot", args.run_id)
    while not stopping():
        try:
            names = [n for n in os.listdir(hd) if not n.endswith(".tmp")]
        except FileNotFoundError:
            time.sleep(1)
            continue
        if not names:
            time.sleep(1)
            continue
        path = os.path.join(hd, rng.choice(names))
        try:
            if rng.random() < 0.3:
                # In a child: a short READ reply from ceph-fuse makes the kernel
                # shrink i_size (fuse_short_read), and touching a mapped page
                # past it is a SIGBUS, which would kill this whole process.
                r = subprocess.run([sys.executable, "-c", HOT_MMAP_READ, path], capture_output=True)
                if r.returncode == -7:
                    bump("node_hot_mmap_sigbus")
                    if counters["node_hot_mmap_sigbus"] <= 5:
                        event("hot_mmap_sigbus", path=path)
                elif r.returncode:
                    raise OSError(r.stderr.decode()[-200:])
                bump("node_hot_mmap_reads")
            else:
                with open(path, "rb") as f:
                    # Drop our cached pages so the read goes to ceph-fuse (readahead
                    # = FUSE background requests) while head keeps rewriting.
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    while f.read(128 * 1024):
                        pass
            bump("node_hot_reads")
        except (OSError, ValueError) as e:
            bump("node_hot_errors")
        time.sleep(rng.uniform(0, args.hot_interval))


def run_node(args):
    threads = [threading.Thread(target=node_worker, args=(i, args), daemon=True) for i in range(args.workers)]
    for i in range(args.hot_readers if args.hot else 0):
        threads.append(threading.Thread(target=node_hot_reader, args=(i, args), daemon=True))
    return threads


# ---------------------------------------------------------------- failcheck


abandoned = []  # probe processes that survived SIGKILL: (Popen, t_abandoned, reported)


def task_report(pid):
    t = "/proc/%d" % pid
    return {"pid": pid, "state": read_proc(t + "/stat").split(") ")[-1][:1], "wchan": read_proc(t + "/wchan"),
            "syscall": read_proc(t + "/syscall")[:60], "stack": read_proc(t + "/stack")[:2000],
            "cmdline": read_proc(t + "/cmdline").replace("\0", " ")[:200]}


def timed_run(cmd, timeout):
    """Run cmd with a timeout; never block on a child that survives SIGKILL
    (a FUSE request already sent waits uninterruptibly for its reply)."""
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = b""
    while p.poll() is None and time.time() - t0 < timeout:
        time.sleep(0.2)
    if p.poll() is None:
        p.kill()
        deadline = time.time() + 5
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if p.poll() is None:
            info = task_report(p.pid)
            abandoned.append([p, time.time(), False])
            event("unkillable_probe", seconds=round(time.time() - t0, 1), **info)
            return "unkillable", "", time.time() - t0
        return -9, "", time.time() - t0
    out = p.stdout.read()
    return p.returncode, out.decode(errors="replace"), time.time() - t0


def check_abandoned():
    for a in list(abandoned):
        p, t, reported = a
        if p.poll() is not None:
            event("unkillable_probe_exited", pid=p.pid, after=round(time.time() - t, 1))
            abandoned.remove(a)
        elif not reported and time.time() - t > 60:
            a[2] = True
            trigger("unkillable_probe", seconds_since_kill=round(time.time() - t), **task_report(p.pid))


def read_proc(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        return "<%s>" % e.strerror


def fusectl_snapshot():
    out = {}
    # A VM has fusectl at the usual place; in a rootless container it is
    # missing (run.py reads the host's copy instead).
    base = "/sys/fs/fuse/connections" if os.listdir("/sys/fs/fuse/connections") else "/fusectl"
    try:
        for c in os.listdir(base):
            out[c] = {k: read_proc("%s/%s/%s" % (base, c, k)) for k in ("waiting", "max_background", "congestion_threshold")}
    except OSError as e:
        out["error"] = repr(e)
    return out


def dstate_tasks():
    tasks = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            tids = os.listdir("/proc/%s/task" % pid)
        except OSError:
            continue
        for tid in tids:
            try:
                with open("/proc/%s/task/%s/stat" % (pid, tid)) as f:
                    stat = f.read()
            except OSError:
                continue
            state = stat[stat.rindex(")") + 2]
            if state == "D":
                comm = stat[stat.index("(") + 1:stat.rindex(")")]
                tasks.append((pid, tid, comm))
    return tasks


DROPPED_CACHE_MD5 = """
import hashlib, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
h = hashlib.md5()
for b in iter(lambda: os.read(fd, 1 << 17), b""):
    h.update(b)
print(h.hexdigest(), sys.argv[1])
"""


def failcheck_probes(args):
    pdir = os.path.join(CTL, "probe")
    seen = set()
    failing_since = None
    last_probe, last_check = None, 0
    while not stopping():
        check_abandoned()
        names = sorted(n for n in os.listdir(pdir) if not n.startswith(".") and n not in seen)
        if names:
            # Only the newest probe matters; mark older ones seen.
            seen.update(names[:-1])
            name = names[-1]
            seen.add(name)
            with open(os.path.join(pdir, name)) as f:
                p = json.load(f)
            last_probe = p
            cmd = ["md5sum", p["path"]]
        elif last_probe and time.time() - last_check > args.probe_interval:
            # No fresh probe (head may be blocked on this client's caps): re-read
            # the last one with our cached pages dropped, so the read still goes
            # out as readahead (FUSE background requests).
            p = last_probe
            cmd = [sys.executable, "-c", DROPPED_CACHE_MD5, p["path"]]
            bump("probe_rechecks")
        else:
            time.sleep(0.5)
            continue
        last_check = time.time()
        age = time.time() - p["t"]
        rc, out, dt = timed_run(cmd, args.read_timeout)
        ok = rc == 0 and out.split()[:1] == [p["md5"]]
        if ok:
            bump("probe_ok")
            if failing_since:
                event("probe_recovered", seq=p["seq"], failed_for=round(time.time() - failing_since, 1))
                failing_since = None
            continue
        bump("probe_buffered_fail")
        rc_d, out_d, dt_d = timed_run(["dd", "if=" + p["path"], "iflag=direct", "bs=1M", "of=/dev/null"], args.read_timeout)
        direct_ok = rc_d == 0
        kind = "buffered_hang_direct_ok" if direct_ok else "buffered_and_direct_fail"
        info = dict(seq=p["seq"], path=p["path"], probe_age=round(age, 1), buffered_rc=rc, buffered_seconds=round(dt, 1),
                    buffered_out=out[:200], direct_rc=rc_d, direct_seconds=round(dt_d, 1), fusectl=fusectl_snapshot())
        bump("probe_" + kind)
        if failing_since is None:
            failing_since = time.time()
            trigger(kind, **info)
        else:
            event("probe_still_failing", kind=kind, failing_for=round(time.time() - failing_since, 1), **info)


def failcheck_dstate(args):
    first_seen = {}
    reported = set()
    while not stopping():
        now = time.time()
        current = {}
        for pid, tid, comm in dstate_tasks():
            key = (pid, tid)
            current[key] = comm
            first_seen.setdefault(key, now)
            if now - first_seen[key] > args.dstate_limit and key not in reported:
                reported.add(key)
                trigger("dstate", pid=pid, tid=tid, comm=comm, seconds=round(now - first_seen[key]),
                        wchan=read_proc("/proc/%s/task/%s/wchan" % (pid, tid)),
                        cmdline=read_proc("/proc/%s/cmdline" % pid).replace("\0", " "),
                        fusectl=fusectl_snapshot())
        for key in list(first_seen):
            if key not in current:
                if key in reported:
                    event("dstate_cleared", pid=key[0], tid=key[1], seconds=round(now - first_seen[key]))
                    reported.discard(key)
                del first_seen[key]
        time.sleep(5)


hung_seen = set()


def failcheck_misc(args):
    dmesg_ok = None
    stalled = False
    while not stopping():
        r = subprocess.run(["dmesg", "--ctime"], capture_output=True)
        if r.returncode:
            if dmesg_ok is not False:
                event("dmesg_unavailable", error=r.stderr.decode().strip())
            dmesg_ok = False
        else:
            hung = [l for l in r.stdout.decode(errors="replace").splitlines() if "blocked for more than" in l]
            new = [l for l in hung if l not in hung_seen]
            if dmesg_ok is None:
                # Lines already in the log when we started belong to earlier runs.
                hung_seen.update(new)
                new = []
            if new:
                hung_seen.update(new)
                trigger("hung_task_dmesg", lines=new[-20:])
            dmesg_ok = True
        # Workload progress stall: node jobs not completing while head produces.
        try:
            with open(os.path.join(CTL, "stats-node.json")) as f:
                st = json.load(f)
            last = max(st.get("last_node_jobs_done", 0), st.get("t0", 0))
            backlog = len(os.listdir(os.path.join(CTL, "ready"))) + len(os.listdir(os.path.join(CTL, "claimed")))
            if last and backlog and time.time() - last > args.stall_limit:
                if not stalled:
                    stalled = True
                    trigger("progress_stall", seconds_since_last_job=round(time.time() - last), backlog=backlog)
            elif stalled:
                stalled = False
                event("progress_resumed")
        except (OSError, ValueError):
            pass
        time.sleep(30)


def run_failcheck(args):
    return [threading.Thread(target=f, args=(args,), daemon=True)
            for f in (failcheck_probes, failcheck_dstate, failcheck_misc)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["head", "node", "failcheck"])
    ap.add_argument("--run-id", default="r%d" % time.time())
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rewrite-p", type=float, default=0.3)
    ap.add_argument("--rewrite-delay", type=float, default=1.0)
    ap.add_argument("--max-backlog", type=int, default=16)
    ap.add_argument("--keep-jobs", type=int, default=40)
    ap.add_argument("--mmap-p", type=float, default=0.25)
    ap.add_argument("--probe-interval", type=float, default=5)
    ap.add_argument("--hot", type=int, default=0, help="number of hot files head keeps rewriting")
    ap.add_argument("--hot-readers", type=int, default=4)
    ap.add_argument("--hot-interval", type=float, default=0.2)
    ap.add_argument("--hot-truncate", action="store_true", help="head also rewrites hot files with O_TRUNC")
    ap.add_argument("--read-timeout", type=int, default=20)
    ap.add_argument("--dstate-limit", type=int, default=60)
    ap.add_argument("--stall-limit", type=int, default=180)
    args = ap.parse_args()
    faulthandler.enable()
    if not os.path.ismount(MNT):
        sys.exit("%s is not mounted" % MNT)
    counters["t0"] = time.time()
    threads = {"head": run_head, "node": run_node, "failcheck": run_failcheck}[args.role](args)
    threads.append(threading.Thread(target=stats_writer, args=(args.role,), daemon=True))
    event("start", role=args.role, args=vars(args))
    for t in threads:
        t.start()
    while not stopping():
        time.sleep(1)
    stop_event.set()
    # Give workers a moment; hung ones stay behind (they are daemon threads).
    deadline = time.time() + 30
    for t in threads:
        t.join(max(0, deadline - time.time()))
    alive = [t.name for t in threads if t.is_alive()]
    event("exit", role=args.role, alive_threads=len(alive), counters=counters)
    os._exit(0)


if __name__ == "__main__":
    main()
