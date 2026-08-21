"""Turn results.jsonl into a readable report.

Reports the median across repetitions rather than the best: the best run of a
client that is usually slow is not what a production node experiences.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict


def load(path):
    meta, rows, failures = {}, [], []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec.get("record")
            if kind == "run_meta":
                meta = rec
            elif kind == "preflight_failure":
                failures.append(rec)
            elif kind == "measurement":
                rows.append(rec)
    return meta, rows, failures


def med(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def collect(rows, workload, field):
    out = defaultdict(list)
    for r in rows:
        block = r.get(workload) or {}
        if isinstance(block, dict) and isinstance(block.get(field), (int, float)):
            out[r["client"]].append(block[field])
    return {k: med(v) for k, v in out.items()}


def table(headers, rows_):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows_)) if rows_ else len(str(h))
              for i, h in enumerate(headers)]
    line = "| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = ["| " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) + " |"
            for r in rows_]
    return "\n".join([line, sep] + body)


def build(path):
    meta, rows, failures = load(path)
    out = []
    a = out.append

    a("# S3 filesystem benchmark results\n")
    a(f"- **Instance**: `{meta.get('instance_type')}` "
      f"({meta.get('cpu_count')} vCPU), kernel `{meta.get('kernel')}`")
    a(f"- **Region/AZ**: {meta.get('region')} / {meta.get('availability_zone')}")
    a(f"- **Corpus**: `{meta.get('corpus_profile')}` profile, "
      f"primary file `{meta.get('primary_file')}`")
    a(f"- **Run profile**: `{meta.get('run_profile')}`")
    a(f"- **Started**: {meta.get('started_utc')}\n")

    if failures:
        a("## Clients that failed preflight\n")
        a(table(["client", "error"],
                [[f["client"], f["error"].replace("\n", " ")[:160]] for f in failures]))
        a("")

    seq = collect(rows, "seq_read", "read_mb_s")
    eff = collect(rows, "seq_read", "effective_mb_s")
    cpu = collect(rows, "seq_read", "cpu_cores_mean")
    ceiling = collect(rows, "seq_read", "copy_mb_s")
    ttfb1 = collect(rows, "ttfb", "first_1mib_ms")
    ttfb64 = collect(rows, "ttfb", "first_64mib_ms")
    p50 = collect(rows, "random_seek", "p50_ms")
    p95 = collect(rows, "random_seek", "p95_ms")
    p99 = collect(rows, "random_seek", "p99_ms")
    agg = collect(rows, "parallel_read", "aggregate_mb_s")
    spread = collect(rows, "parallel_read", "stream_spread_ratio")
    stats = collect(rows, "metadata", "stat_per_second")

    # Rank and normalise on the end-to-end number for each client. For the
    # copy baseline that is `effective` (S3 -> disk -> app); its raw read_mb_s
    # is a local NVMe read and is not an S3 measurement at all, so including it
    # in the ceiling would understate every mount against a number no mount
    # is competing for.
    def endtoend(k):
        return eff.get(k) or seq.get(k) or 0
    order = sorted(seq, key=lambda k: -endtoend(k))
    best = max([endtoend(k) for k in seq] or [0])

    a("## Headline: bulk sequential read\n")
    a("The number that matters for pulling hour-long files onto a node. "
      "Rows are ranked on **end-to-end MB/s**, the rightmost throughput column.\n")
    a("For mount-based clients the two throughput columns are the same number. "
      "For the copy-based baseline they are not, and the difference matters: "
      "its raw read is a **local SSD** read of a file that has already been "
      "downloaded, so it is not an S3 measurement at all and must not be "
      "compared against a mount. Such rows are marked `(local disk)`, and their "
      "end-to-end figure charges both the download and the read.\n")
    a(table(
        ["client", "raw read MB/s", "end-to-end MB/s", "Gbit/s", "% of best", "CPU cores"],
        [[k,
          f"{seq[k]} (local disk)" if (k in eff and eff.get(k)) else seq.get(k, "-"),
          round(endtoend(k), 1) if endtoend(k) else "-",
          round(endtoend(k) * 8 / 1000, 2) if endtoend(k) else "-",
          f"{100*endtoend(k)/best:.0f}%" if best else "-",
          cpu.get(k, "-")] for k in order]))
    a("")

    # What the instance can actually do, taken from the fastest thing observed
    # rather than from the copy baseline.
    #
    # The copy baseline's rate is NOT a ceiling. It is S3 -> instance-store
    # write, and on a file larger than RAM the instance store's write bandwidth
    # is the binding constraint, not S3. Mounts routinely beat it because they
    # never write to disk at all -- they stream into page cache and on to the
    # reading process. Reporting it as a ceiling made every mount look like it
    # had exceeded the hardware, which is how this bug was spotted.
    observed = []
    for r in rows:
        if r.get("kind") == "copy":
            continue
        for wl, fld in (("seq_read", "read_mb_s"), ("parallel_read", "aggregate_mb_s")):
            v = (r.get(wl) or {}).get(fld)
            if isinstance(v, (int, float)):
                observed.append((v, r["client"], wl))
    if observed:
        top, who, how = max(observed)
        a(f"Highest throughput observed on this instance: **{top:.0f} MB/s "
          f"({top*8/1000:.1f} Gbit/s)**, by `{who}` on {how.replace('_', ' ')}. "
          f"Since the network allowance counters below move during the fastest "
          f"runs, treat that as the instance's network limit rather than any "
          f"client's limit.\n")

    if ceiling:
        disk = max(v for v in ceiling.values() if v)
        faster = [c for c, v in seq.items() if v and v > disk]
        cbytes = med(collect(rows, "seq_read", "copy_bytes").values()) \
            if collect(rows, "seq_read", "copy_bytes") else None
        mem = meta.get("mem_total_bytes")
        if mem and cbytes and cbytes < mem * 0.8:
            why = ("the object was smaller than this node's %.0f GB of RAM, so "
                   "page cache absorbed the write and this is really an "
                   "S3-to-memory rate. It will not hold on files larger than "
                   "memory." % (mem / 1e9))
        elif mem and cbytes:
            why = ("the object was larger than this node's %.0f GB of RAM, so "
                   "the instance store's write bandwidth binds first." % (mem / 1e9))
        elif cbytes:
            why = ("at a %.0f GB object it is bounded by whichever of S3, page "
                   "cache and instance-store write is slowest -- on this class "
                   "of instance, writes to the instance store usually bind "
                   "first once the object exceeds RAM." % (cbytes / 1e9))
        else:
            why = ("it is bounded by whichever of S3, page cache and "
                   "instance-store write is slowest at the object size used.")
        plural = "configuration" if len(faster) == 1 else "configurations"
        a(f"For contrast, the copy baseline moved S3 to instance-store at "
          f"**{disk:.0f} MB/s**. That is not an S3 ceiling: {why}"
          + (f" {len(faster)} mount {plural} beat it, because a mount never "
             f"writes the object to disk.\n" if faster else "\n"))
    a("")

    a("## Time to first bytes\n")
    a("Latency to start decoding. A client can win on throughput and still be "
      "the wrong choice if nothing is readable for a minute.\n")
    a(table(["client", "first 1 MiB (ms)", "first 64 MiB (ms)"],
            [[k, ttfb1.get(k, "-"), ttfb64.get(k, "-")]
             for k in sorted(ttfb1, key=lambda x: ttfb1.get(x) or 1e9)]))
    a("")

    a("## Random seek (8 MiB reads at random offsets)\n")
    a(table(["client", "p50 ms", "p95 ms", "p99 ms"],
            [[k, p50.get(k, "-"), p95.get(k, "-"), p99.get(k, "-")]
             for k in sorted(p50, key=lambda x: p50.get(x) or 1e9)]))
    a("")

    a("## Concurrent streams\n")
    a("`spread` is fastest stream / slowest stream. Well above 1.0 means one "
      "stream starved the others, which matters when workers share a node.\n")
    a(table(["client", "aggregate MB/s", "aggregate Gbit/s", "spread"],
            [[k, agg.get(k, "-"),
              round(agg[k] * 8 / 1000, 2) if agg.get(k) else "-",
              spread.get(k, "-")]
             for k in sorted(agg, key=lambda x: -(agg.get(x) or 0))]))
    a("")

    if stats:
        a("## Metadata (stat/s)\n")
        a(table(["client", "stat/s"],
                [[k, stats.get(k, "-")]
                 for k in sorted(stats, key=lambda x: -(stats.get(x) or 0))]))
        a("")

    # A random-read workload that moved far less over the wire than it claimed
    # to read was served from page cache, not from S3. That happens when the
    # test file is smaller than node RAM and a client prefetches it wholesale,
    # and it makes that client's seek latency look impossibly good.
    cached = []
    for r in rows:
        rs = r.get("random_seek") or {}
        want = (rs.get("reads") or 0) * (rs.get("read_size_bytes") or 0)
        got = rs.get("nic_rx_bytes")
        if want and got is not None and got < want * 0.5:
            cached.append((r["client"], r["rep"], got, want, rs.get("p50_ms")))

    # Validity warnings are part of the report, not a footnote: a throughput
    # number taken while the instance was throttled is not a throughput number.
    warnings = []
    for r in rows:
        for wl in ("seq_read", "parallel_read", "random_seek"):
            ex = (r.get(wl) or {}).get("ena_allowance_exceeded") or {}
            for counter, delta in ex.items():
                warnings.append(f"`{r['client']}` rep{r['rep']} {wl}: "
                                f"{counter} +{delta}")
    a("## Validity checks\n")
    if cached:
        a("**Some random-seek measurements were served from page cache, not S3.** "
          "These clients moved far fewer bytes over the network than they read, "
          "which means the test file fit in node RAM and was prefetched. Their "
          "seek latencies below are not S3 latencies. Use the `prod` corpus, "
          "whose files are larger than RAM, for trustworthy seek numbers.\n")
        a(table(["client", "rep", "read (MB)", "over network (MB)", "reported p50 ms"],
                [[c, rep, round(want/1e6), round(got/1e6), p50]
                 for c, rep, got, want, p50 in cached[:20]]))
        a("")
    if warnings:
        a("**Network allowance was exceeded during these measurements.** The "
          "instance hit an EC2 network limit, so the affected numbers describe "
          "the instance, not the client. Re-run on a type with sustained "
          "bandwidth before trusting the ranking of the fastest clients.\n")
        for w in warnings[:40]:
            a(f"- {w}")
    else:
        a("- No ENA allowance-exceeded counters moved during the run: no "
          "evidence of network throttling.")
    a("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results/results.jsonl")
    ap.add_argument("--out", default="results/report.md")
    args = ap.parse_args()
    text = build(args.results)
    with open(args.out, "w") as fh:
        fh.write(text)
    print(text)
    print(f"\n[written to {args.out}]", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
