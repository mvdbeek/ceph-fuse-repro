<!--
Draft for https://tracker.ceph.com/projects/cephfs/issues/new
  Tracker:           Bug
  Subject:           client: ObjectCacher loses a read when a discard (fallocate punch hole) hits an in-flight read and the zeroed bh is trimmed before the reply
  Component(FS):     Client, ceph-fuse
  Affected versions: v17.2.9
-->

## Summary

`ObjectCacher::Object::discard()` turns a BufferHead with a read in flight (rx) into a zero bh, but the read's `waitfor_read` contexts stay attached to it. Only rx/tx bhs are pinned in `bh_lru_rest`, so `trim()` or `release()` can delete the zero bh, waiters included, before the OSD reply arrives. `bh_read_finish()` then finds no bh for the range, and the read never completes.

In ceph-fuse this leaves a FUSE read request that is never answered:

- The page stays locked and the reader is stuck in D state. Further buffered reads queue behind it, and eventually block in `fuse_get_req` once the background slots fill.
- A later MDS-driven invalidate (`fn_anonymous` → `fuse_reverse_inval_inode`) blocks on the locked folio.
- The read's `Fc` cap reference is never dropped, so other clients block on that file.
- `kill -9` of ceph-fuse leaves a zombie, because the invalidate thread is in D. Only `/sys/fs/fuse/connections/<id>/abort` releases the mount.

Throughout, the cluster stays HEALTH_OK, and there are no outstanding objecter or MDS ops.

This matches a stall we see in production on a Rocky 9 node running ceph-fuse 17.2.9 on kernel 6.1. We have not confirmed that a punch hole is the trigger there.

## Code path (as of main)

- `src/osdc/ObjectCacher.cc`, `ObjectCacher::Object::discard()`, rx branch:
  - It bumps `last_read_tid`, clears `bl`, calls `mark_zero(bh)` and `continue`s, keeping `bh->waitfor_read`.
  - For bhs in other states, it moves the waiters to `waiting_for_read` and restarts them with `finish_contexts()`.
- `ObjectCacher::trim()` deletes zero/clean bhs from `bh_lru_rest`, and `ObjectCacher::release()` deletes zero/clean/error bhs. Neither looks at `waitfor_read`.
- `ObjectCacher::bh_read_finish()` only wakes waiters of bhs that are still in `ob->data`.

Where the discard comes from in the client:

- `Client::_fallocate()` with `FALLOC_FL_PUNCH_HOLE` calls `_invalidate_inode_cache(in, offset, length)`, which calls `objectcacher->discard_writeback()`. Only Fw/Fb are needed for this, so it is not ordered after the client's own read in flight.
- Truncate (`update_inode_file_size`) reaches the same discard. In our runs the cap protocol ordered it after the read: the Fr revoke waited for the read's FILE_RD ref, and the reply arrived before the discard.

## Reproducer

This is deterministic with stock ceph-fuse 17.2.9 (Rocky 9 el9 packages) against a small containerized 17.2.9 cluster. The client kernel made no difference: 6.1.188 and 7.x behave the same.

1. Mount with ceph-fuse, with `client_oc_size = 2097152` (a small cache makes a trim easy to force) and `debug_objectcacher = 10`.
2. Create a 4 MiB file `F` whose first object maps to `osd.N`, plus a few 512 KiB files on other OSDs.
3. Hold `osd.N`'s replies to the client. On the `osd.N` host:

   ```
   tc qdisc add dev eth0 root handle 1: prio
   tc filter add dev eth0 parent 1: protocol ip prio 1 basic match 'cmp(u32 at 16 layer network eq <client-ip-hex>)' action drop
   ```

   TCP retransmits, so the session does not fail.
4. Start a read: `dd if=F of=/dev/null bs=64k count=1 &`. The read is now in flight, with an rx bh and a waiter.
5. From the same mount, punch a hole: `fallocate -p -o 0 -l 4194304 F &`. The client discards the range immediately (`discard … bh[… rx] waiters = { 0->[…] }`). The punch's own OSD op is held too.
6. Read about 6 MiB of the other files through the same mount, for example with `POSIX_FADV_RANDOM`. `_readx()` → `trim()` deletes the zeroed bh (`trim trimming bh[… zero] waiters = { 0->[…] }`).
7. Remove the tc filter. The reply arrives (`bh_read_finish … returned 0`), but `dd` never returns.

Log from such a run (17.2.9, packaged ceph-fuse):

```
14:47:06.260 objectcacher.object(10000000261.00000000/head) discard object[10000000261.00000000/head oset 0x794e3c00a8a0 wr 0/0] bh bh[ 0x794e24008280 0~131072 0x794e24008070 (0) v 0 rx] waiters = { 0->[0x794e24008fa0, ]}
14:47:10.375 objectcacher trim trimming bh[ 0x794e24008280 0~131072 0x794e24008070 (0) v 0 zero] waiters = { 0->[0x794e24008fa0, ]}
14:47:15.139 objectcacher bh_read_finish 10000000261.00000000/head tid 2 0~131072 (bl is 131072) returned 0 outstanding reads 1
```

Afterwards, `dd`, `fallocate` and ceph-fuse's `fn_anonymous` thread are all in D in `folio_wait_bit_common`, and another client reading `F` hangs.

With 12 such files in flight at once, the 12 lost reads fill `max_background` (12). On a 6.1.188 client:

- buffered reads of any uncached file hang in `fuse_get_req`;
- `stat`, O_DIRECT reads and writes still work;
- this hung-task report never clears:

```
INFO: task fn_anonymous:5346 blocked for more than 122 seconds.
 folio_wait_bit_common+0x120/0x360
 invalidate_inode_pages2_range+0x12f/0x380
 fuse_reverse_inval_inode+0x93/0xc0 [fuse]
 fuse_notify+0x3b6/0x520 [fuse]
 fuse_dev_do_write+0x305/0x4e0 [fuse]
```

| Variant | Outcome |
|---|---|
| punch hole + churn, 1 file | 5 of 5 runs lost the read |
| no punch hole, or no churn (controls) | all reads complete |
| truncate + churn | all reads complete |
| punch hole + churn, 12 files | 12 of 12 reads lost (6.1.188 and 7.x) |

The harness and the run logs are at https://github.com/mvdbeek/ceph-fuse-repro (`harness/oc_lostread.sh`; the results are written up in `results.md`, Findings 7 and 8).

## A possible fix

https://github.com/mvdbeek/ceph/tree/wip-objectcacher-discard-rx has a patch against main, offered in case it helps. In the rx branch of `discard()`, it restarts the waiters the way the other branch already does:

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

The retried `_readx()` finds the zero bh and returns zeros, and the bumped `last_read_tid` already makes the late reply a no-op. The branch also adds `unittest_object_cacher` (gtest), whose `WritebackHandler` holds reads until the test answers them:

| Test | v17.2.9 | With the patch |
|---|---|---|
| read in flight, discard, trim, reply | FAILED (read never completes) | OK |
| read in flight, discard, release, reply | FAILED (read never completes) | OK |
| read in flight, discard, reply | OK | OK |

With the same change applied to 17.2.9, the reproducer above no longer hangs. The race fired in every run (3 of 3 single-file runs; 12 of 12 reads in the 12-file run), and each read was answered with zeros at the moment of the discard, while the OSD replies were still held. On the same cluster and in the same session, the packaged 17.2.9 ceph-fuse hung.

The patch was built and tested on 17.2.9 only. It applies to main, where the code it touches is the same, but I have not built main.
