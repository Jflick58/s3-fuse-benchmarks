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
      "`effective` charges the copy-based baseline for its download, which is "
      "the honest comparison against a mount that streams while you read.\n")
    a(table(
        ["client", "MB/s", "Gbit/s", "effective MB/s", "% of best", "CPU cores"],
        [[k,
          seq.get(k, "-"),
          round((seq[k] * 8 / 1000), 2) if seq.get(k) else "-",
          eff.get(k, "-"),
          f"{100*endtoend(k)/best:.0f}%" if best else "-",
          cpu.get(k, "-")] for k in order]))
    a("")

    if ceiling:
        top = max(v for v in ceiling.values() if v)
        a(f"Raw parallel-GET ceiling measured on this instance: **{top:.0f} MB/s "
          f"({top*8/1000:.1f} Gbit/s)**. Treat that as what the hardware can do; "
          f"a mount's gap to it is the cost of the filesystem layer.\n")

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
