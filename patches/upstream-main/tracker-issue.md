<!--
Draft for https://tracker.ceph.com/projects/cephfs/issues/new
  Tracker:           Bug
  Subject:           client: ObjectCacher loses a read when a discard (fallocate punch hole) hits an in-flight read and the zeroed bh is trimmed before the reply
  Category:          Correctness/Safety
  Component(FS):     Client, ceph-fuse
  Severity:          2 - major
  Affected versions: v17.2.9 (code unchanged on main)
  Pull request ID:   (fill in after opening the PR from mvdbeek:wip-objectcacher-discard-rx)
-->

## Summary

`ObjectCacher::Object::discard()` turns a BufferHead with a read in flight (rx) into a zero bh, but leaves the read's `waitfor_read` contexts on it. A zero bh is not pinned in `bh_lru_rest`; only rx/tx bhs are. So `trim()` or `release()` can delete it, waiters included, before the OSD reply arrives. `bh_read_finish()` then finds no bh for the range, and the read never completes.

In ceph-fuse this leaves a FUSE read request that is never answered:

- The page stays locked. The reader is stuck in D state. On the client kernel, buffered reads queue behind it and eventually block in `fuse_get_req` once the background slots fill.
- A later MDS-driven invalidate (`fn_anonymous` → `fuse_reverse_inval_inode`) blocks on the locked folio.
- The read's `Fc` cap reference is never dropped, so other clients block on that file.
- `kill -9` of ceph-fuse leaves a zombie, because the invalidate thread is in D. Only `/sys/fs/fuse/connections/<id>/abort` releases the mount.

Meanwhile the cluster is HEALTH_OK, and there are no outstanding objecter or MDS ops.

This matches a production stall we see on a Rocky 9 node running ceph-fuse 17.2.9 on kernel 6.1. We have not yet confirmed that the production trigger is a punch hole.

## Root cause (code references on main)

- `src/osdc/ObjectCacher.cc`, `ObjectCacher::Object::discard()`, rx branch. It bumps `last_read_tid`, clears `bl`, calls `mark_zero(bh)` and `continue`s. `bh->waitfor_read` is kept. The other branch (non-rx, non-tx) moves the waiters to `waiting_for_read` and restarts them with `finish_contexts()`.
- `ObjectCacher::trim()` deletes zero/clean bhs from `bh_lru_rest`, and `ObjectCacher::release()` deletes zero/clean/error bhs. Neither looks at `waitfor_read`.
- `ObjectCacher::bh_read_finish()` only wakes waiters of bhs that are still in `ob->data`.

How the client reaches the discard:

- `Client::_fallocate()` with `FALLOC_FL_PUNCH_HOLE` calls `_invalidate_inode_cache(in, offset, length)`, which calls `objectcacher->discard_writeback()`. This only needs Fw/Fb caps, so nothing orders it after the client's own read in flight.
- Truncate (`update_inode_file_size`) goes through the same discard, but there the cap protocol orders it after the read: Fr is revoked, and the revoke waits for the read's FILE_RD ref. In our runs the read reply always arrived before the truncate's discard.

## Reproducer

This is deterministic, with stock ceph-fuse 17.2.9 (Rocky 9 el9 packages) against a small containerized 17.2.9 cluster. It does not depend on the client kernel: we see the same result on 6.1.188 and on 7.x.

1. Mount with ceph-fuse. Set `client_oc_size = 2097152` (a small cache makes trimming easy to force) and `debug_objectcacher = 10`.
2. Create a 4 MiB file `F` whose first object maps to `osd.N`, plus a few 512 KiB files on other OSDs.
3. Hold the OSD replies to the client. On the `osd.N` host, drop `osd.N`'s egress packets to the client's IP, e.g. `tc qdisc add dev eth0 root handle 1: prio; tc filter add dev eth0 parent 1: protocol ip prio 1 basic match 'cmp(u32 at 16 layer network eq <client-ip-hex>)' action drop`. TCP retransmits, so the session does not fail.
4. Run `dd if=F of=/dev/null bs=64k count=1 &`. The read is now in flight, with an rx bh and a waiter.
5. From the same mount, run `fallocate -p -o 0 -l 4194304 F &`. The client discards the range at once (the log shows `discard … bh[… rx] waiters = { 0->[…] }`). The punch's own OSD op is held too.
6. Read about 6 MiB of the other files through the same mount, for example with `POSIX_FADV_RANDOM` reads. `_readx()` → `trim()` deletes the zeroed bh (`trim trimming bh[… zero] waiters = { 0->[…] }`).
7. Remove the tc filter. The reply arrives (`bh_read_finish … returned 0`), but the `dd` never returns.

Log from such a run (17.2.9, packaged ceph-fuse):

```
14:47:06.260 objectcacher.object(10000000261.00000000/head) discard object[10000000261.00000000/head oset 0x794e3c00a8a0 wr 0/0] bh bh[ 0x794e24008280 0~131072 0x794e24008070 (0) v 0 rx] waiters = { 0->[0x794e24008fa0, ]}
14:47:10.375 objectcacher trim trimming bh[ 0x794e24008280 0~131072 0x794e24008070 (0) v 0 zero] waiters = { 0->[0x794e24008fa0, ]}
14:47:15.139 objectcacher bh_read_finish 10000000261.00000000/head tid 2 0~131072 (bl is 131072) returned 0 outstanding reads 1
```

After this, `dd`, `fallocate` and ceph-fuse's `fn_anonymous` thread are all in D in `folio_wait_bit_common`; another client reading `F` hangs.

With 12 such files in flight at once, the 12 lost reads fill `max_background` (12). On a 6.1.188 client the result is exactly our production signature:

- buffered reads of any uncached file hang in `fuse_get_req`;
- `stat`, O_DIRECT reads and writes still work;
- the hung-task report never clears:

```
INFO: task fn_anonymous:5346 blocked for more than 122 seconds.
 folio_wait_bit_common+0x120/0x360
 invalidate_inode_pages2_range+0x12f/0x380
 fuse_reverse_inval_inode+0x93/0xc0 [fuse]
 fuse_notify+0x3b6/0x520 [fuse]
 fuse_dev_do_write+0x305/0x4e0 [fuse]
```

Results:

| Variant | Outcome |
|---|---|
| punch hole + churn, 1 file | 5 of 5 runs lost the read |
| no punch hole, or no churn (controls) | all reads complete |
| truncate + churn | all reads complete (protected by caps) |
| punch hole + churn, 12 files | 12 of 12 reads lost, on both kernels |

## Fix

PR: https://github.com/mvdbeek/ceph/tree/wip-objectcacher-discard-rx (to be opened against main)

The fix restarts the waiters of an rx bh in `discard()`, as is already done for bhs in other states. The retried `_readx()` finds the zero bh and returns zeros, which is the correct content of a discarded range. The late reply is already ignored because of the bumped `last_read_tid`.

```cpp
      oc->mark_zero(bh);
      // a zero bh is not pinned, so trim() or release() may delete it
      // before the read reply arrives, and bh_read_finish() could then not
      // find the waiters. restart them now; they will see the zeroed range.
      for ([[maybe_unused]] auto& [off, ctxs] : bh->waitfor_read) {
        waiting_for_read.splice(waiting_for_read.end(), ctxs);
      }
      bh->waitfor_read.clear();
```

The PR also adds `unittest_object_cacher` (gtest). Its `WritebackHandler` holds reads until the test answers them, so the read → discard → trim/release → reply order is exact:

| Test | v17.2.9 | With the fix |
|---|---|---|
| read in flight, discard, trim, reply | FAILED (read never completes) | OK |
| read in flight, discard, release, reply | FAILED (read never completes) | OK |
| read in flight, discard, reply | OK | OK |

With the fix backported to 17.2.9, the reproducer above no longer hangs. The race fired in every run: 3 of 3 single-file runs and 12 of 12 reads in the 12-file run. Each read was answered with zeros at the moment of the discard, while the OSD replies were still held. The packaged 17.2.9 ceph-fuse hung on the same cluster in the same session.

Note: the patches were built and tested on v17.2.9. They apply cleanly to main, and the APIs they touch are unchanged there, but I have not built main.

The fix should be backported to the supported releases (reef, squid, tentacle).
