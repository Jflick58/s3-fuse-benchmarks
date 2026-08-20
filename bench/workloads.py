"""The measurements themselves.

Ordered roughly by how much they matter for pulling hour-long video files onto
a GPU node: bulk sequential throughput first, then how long until the first
frame is available, then seek behaviour, concurrency, and metadata.
"""

import concurrent.futures
import os
import shutil
import random
import statistics
import subprocess
import time

import clients as C
from metrics import Sampler, drop_caches

CHUNK = 8 * 1024 * 1024


def _pid(client):
    return client.proc.pid if client.proc else None


def _read_stream(path, max_bytes=None, chunk=CHUNK):
    """Read a file to nowhere, returning bytes consumed."""
    total = 0
    with open(path, "rb", buffering=0) as fh:
        while True:
            if max_bytes is not None and total >= max_bytes:
                break
            want = chunk if max_bytes is None else min(chunk, max_bytes - total)
            block = fh.read(want)
            if not block:
                break
            total += len(block)
    return total


def _object_size(bucket, key):
    out = subprocess.run(["s5cmd", "ls", f"s3://{bucket}/{key}"],
                         capture_output=True, text=True)
    for field in reversed(out.stdout.split()):
        if field.isdigit():
            return int(field)
    return 0


def _ensure_space(client, mount_dir, key, bucket):
    """Make room on the instance store before staging another object.

    The prod corpus is larger than the instance store on smaller node types, so
    without this the copy baseline fills the disk partway through a run and
    every later measurement fails for a reason that looks like a client bug.
    Previously staged files are evicted first; their recorded copy costs stay
    valid because each was measured when it was actually downloaded.
    """
    need = _object_size(bucket, key)
    if not need:
        return
    free = shutil.disk_usage(mount_dir).free
    if free > need * 1.1:
        return

    for stale in sorted(os.listdir(mount_dir)):
        path = os.path.join(mount_dir, stale)
        if os.path.isfile(path) and os.path.basename(key) != stale:
            os.unlink(path)
            if shutil.disk_usage(mount_dir).free > need * 1.1:
                return

    free = shutil.disk_usage(mount_dir).free
    if free <= need * 1.1:
        raise RuntimeError(
            f"instance store has {free/1e9:.0f} GB free but staging "
            f"{os.path.basename(key)} needs {need/1e9:.0f} GB. Use a node type "
            f"with a larger instance store, or a smaller corpus profile.")


def _rate(nbytes, seconds):
    if seconds <= 0:
        return None, None
    mbps = nbytes / seconds / 1e6
    return round(mbps, 1), round(nbytes * 8 / seconds / 1e9, 2)


def materialize(client, bucket, key, mount_dir):
    """Resolve a corpus key to a readable local path.

    For mount-based clients this is just a path join. For the copy-based
    baseline it performs the download and reports what that cost, because the
    download IS that architecture's read latency.

    The cost is remembered per key. Later workloads in the same repetition
    reuse the downloaded file rather than re-copying it, but still get charged
    the original copy time -- otherwise time-to-first-byte for this client
    would come back near zero purely because an earlier workload had already
    paid for the download.
    """
    if client.kind != C.COPY:
        return os.path.join(mount_dir, key), 0.0, 0

    if not hasattr(client, "_copy_cost"):
        client._copy_cost = {}
    dst = os.path.join(mount_dir, os.path.basename(key))
    if os.path.exists(dst) and key in client._copy_cost:
        secs, nbytes = client._copy_cost[key]
        return dst, secs, nbytes

    _ensure_space(client, mount_dir, key, bucket)
    t0 = time.monotonic()
    proc = subprocess.run(
        ["s5cmd", "--numworkers", "64", "cp", "-c", "32", "-p", "32",
         f"s3://{bucket}/{key}", dst],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"s5cmd copy failed: {proc.stderr[-500:]}")
    secs, nbytes = time.monotonic() - t0, os.path.getsize(dst)
    client._copy_cost[key] = (secs, nbytes)
    return dst, secs, nbytes


# --------------------------------------------------------------------------

def seq_read(client, ctx, key):
    """Bulk sequential throughput -- the headline number for large video files."""
    drop_caches()
    with Sampler(_pid(client)) as s:
        path, copy_s, copy_bytes = materialize(client, ctx.bucket, key, ctx.mount_dir)
        t0 = time.monotonic()
        nbytes = _read_stream(path, ctx.params["seq_max_bytes"])
        read_s = time.monotonic() - t0

    mbps, gbps = _rate(nbytes, read_s)
    out = {
        "bytes": nbytes,
        "read_seconds": round(read_s, 3),
        "read_mb_s": mbps,
        "read_gbit_s": gbps,
        **s.summary(),
    }
    if client.kind == C.COPY:
        # copy_bytes is the whole object, but the read may be capped well below
        # that on the prod corpus. Combining the two rates rather than the two
        # durations keeps the comparison size-independent: charging a full
        # 100 GB download against a capped 20 GB read would understate this
        # client by 5x for reasons that have nothing to do with S3.
        copy_mbps, _ = _rate(copy_bytes, copy_s)
        eff = None
        if copy_mbps and mbps:
            eff = round(1.0 / (1.0 / copy_mbps + 1.0 / mbps), 1)
        out.update({
            "copy_seconds": round(copy_s, 3),
            "copy_bytes": copy_bytes,
            # The raw parallel-GET rate, and the closest thing in this suite to
            # the instance's achievable S3 ceiling.
            "copy_mb_s": copy_mbps,
            "effective_mb_s": eff,
            "effective_gbit_s": round(eff * 8 / 1000, 2) if eff else None,
            "note": "read_mb_s is local NVMe; copy_mb_s is the S3 ceiling; "
                    "effective_mb_s combines both phases",
        })
    return out


def ttfb(client, ctx, key):
    """How long until the first bytes, and until enough to start decoding."""
    drop_caches()
    path, copy_s, _ = materialize(client, ctx.bucket, key, ctx.mount_dir)

    t0 = time.monotonic()
    with open(path, "rb", buffering=0) as fh:
        fh.read(1024 * 1024)
        first_mib = time.monotonic() - t0
        got = 1024 * 1024
        while got < 64 * 1024 * 1024:
            block = fh.read(CHUNK)
            if not block:
                break
            got += len(block)
        first_64mib = time.monotonic() - t0

    out = {
        "first_1mib_ms": round(first_mib * 1000, 1),
        "first_64mib_ms": round(first_64mib * 1000, 1),
    }
    if client.kind == C.COPY:
        out.update({
            "copy_seconds": round(copy_s, 3),
            "first_1mib_ms": round((copy_s + first_mib) * 1000, 1),
            "first_64mib_ms": round((copy_s + first_64mib) * 1000, 1),
            "note": "nothing is readable until the whole object has landed",
        })
    return out


def random_seek(client, ctx, key):
    """Scrub/keyframe-seek behaviour: many small reads at random offsets."""
    drop_caches()
    path, copy_s, _ = materialize(client, ctx.bucket, key, ctx.mount_dir)
    size = os.path.getsize(path)
    n = ctx.params["random_reads"]
    rsize = ctx.params["random_read_size"]
    rng = random.Random(1729)   # fixed seed: every client sees identical offsets

    lat = []
    with Sampler(_pid(client)) as s:
        with open(path, "rb", buffering=0) as fh:
            for _ in range(n):
                off = rng.randrange(0, max(1, size - rsize))
                t0 = time.monotonic()
                fh.seek(off)
                fh.read(rsize)
                lat.append((time.monotonic() - t0) * 1000)

    lat.sort()
    def pct(p):
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else None

    out = {
        "reads": n,
        "read_size_bytes": rsize,
        "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
        "mean_ms": round(statistics.fmean(lat), 1) if lat else None,
        **s.summary(),
    }
    if client.kind == C.COPY:
        out["note"] = "post-copy local latency; add %.1fs for the copy" % copy_s
    return out


def parallel_read(client, ctx, keys):
    """Several concurrent whole-file streams, as with multiple workers per node."""
    drop_caches()
    streams = min(ctx.params["parallel_streams"], len(keys))
    if streams < 2:
        return {"skipped": "needs at least 2 corpus files"}
    if client.kind == C.COPY:
        # Release whatever the earlier workloads staged; this workload needs
        # room for several files at once.
        for stale in os.listdir(ctx.mount_dir):
            path = os.path.join(ctx.mount_dir, stale)
            if os.path.isfile(path):
                os.unlink(path)
        client._copy_cost = {}
    per_stream = max(ctx.params["seq_max_bytes"] // streams, 256 * 1024 * 1024)

    def one(k):
        path, copy_s, _ = materialize(client, ctx.bucket, k, ctx.mount_dir)
        t0 = time.monotonic()
        n = _read_stream(path, per_stream)
        return n, time.monotonic() - t0 + copy_s

    with Sampler(_pid(client)) as s:
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=streams) as pool:
            results = list(pool.map(one, keys[:streams]))
        wall = time.monotonic() - t0

    total = sum(r[0] for r in results)
    per = [v for v in (_rate(n, sec)[0] for n, sec in results) if v]
    agg_mbps, agg_gbps = _rate(total, wall)
    return {
        "streams": streams,
        "bytes": total,
        "wall_seconds": round(wall, 3),
        "aggregate_mb_s": agg_mbps,
        "aggregate_gbit_s": agg_gbps,
        "per_stream_mb_s": per,
        # A large spread means one stream starved the others, which matters
        # when several decoders share a node.
        "stream_spread_ratio": round(max(per) / min(per), 2) if per and min(per) else None,
        **s.summary(),
    }


def metadata(client, ctx):
    """stat and listdir rates. Not the bottleneck for big files, but s3fs's
    metadata behaviour is often what makes a mount feel broken."""
    if client.kind == C.COPY:
        return {"skipped": "no filesystem namespace to walk"}
    drop_caches()
    small_dir = os.path.join(ctx.mount_dir, "small")
    if not os.path.isdir(small_dir):
        return {"skipped": "no small-object prefix in corpus"}

    t0 = time.monotonic()
    names = sorted(os.listdir(small_dir))
    list_s = time.monotonic() - t0

    n = min(ctx.params["metadata_ops"], len(names))
    t0 = time.monotonic()
    for name in names[:n]:
        os.stat(os.path.join(small_dir, name))
    stat_s = time.monotonic() - t0

    return {
        "listdir_entries": len(names),
        "listdir_seconds": round(list_s, 3),
        "stat_count": n,
        "stat_seconds": round(stat_s, 3),
        "stat_per_second": round(n / stat_s, 1) if stat_s > 0 else None,
    }
