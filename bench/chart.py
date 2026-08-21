"""Render results.jsonl into SVG bar charts for the README.

Plain SVG with no external libraries, because GitHub sanitises embedded CSS
and will not run scripts. Colours are chosen to stay legible on both the light
and dark README backgrounds rather than relying on a media query.
"""

import argparse
import json
import statistics
from collections import defaultdict

# Muted mid-tones read acceptably against white and against #0d1117.
PALETTE = {
    "mountpoint": "#2f81f7",
    "geesefs":    "#3fb950",
    "s3fs":       "#db6d28",
    "rclone":     "#a371f7",
    "FSx":        "#e3b341",
    "s5cmd":      "#8b949e",
}
AXIS = "#8b949e"
LABEL = "#8b949e"


def family_colour(client):
    for k, v in PALETTE.items():
        if client.lower().startswith(k.lower()):
            return v
    return "#8b949e"


def collect(path, workload, field, lower_is_better=False):
    vals = defaultdict(list)
    for line in open(path):
        if '"measurement"' not in line:
            continue
        r = json.loads(line)
        block = r.get(workload) or {}
        v = block.get(field)
        if isinstance(v, (int, float)):
            vals[r["client"]].append(v)
    out = {k: statistics.median(v) for k, v in vals.items() if v}
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=not lower_is_better))


def bar_chart(data, title, unit, out_path, note=None, log=False):
    if not data:
        return
    rows = list(data.items())
    row_h, pad_top, pad_left, pad_right = 30, 58, 190, 90
    height = pad_top + row_h * len(rows) + (34 if note else 14)
    width = 780
    plot_w = width - pad_left - pad_right
    peak = max(v for _, v in rows) or 1

    def bar_len(v):
        if not log:
            return plot_w * (v / peak)
        import math
        lo = min(x for _, x in rows if x > 0) or 1
        return plot_w * (math.log10(max(v, lo) / lo) / max(math.log10(peak / lo), 0.0001))

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">']
    p.append(f'<text x="14" y="26" font-size="15" font-weight="600" fill="{LABEL}">{title}</text>')
    p.append(f'<text x="14" y="44" font-size="11.5" fill="{AXIS}">{unit}</text>')

    for i, (name, val) in enumerate(rows):
        y = pad_top + i * row_h
        w = max(bar_len(val), 2)
        p.append(f'<text x="{pad_left-10}" y="{y+15}" font-size="12" text-anchor="end" fill="{LABEL}">{name}</text>')
        p.append(f'<rect x="{pad_left}" y="{y+3}" width="{w:.1f}" height="18" rx="3" fill="{family_colour(name)}"/>')
        shown = f"{val:,.0f}" if val >= 10 else f"{val:,.1f}"
        p.append(f'<text x="{pad_left+w+8:.1f}" y="{y+16}" font-size="12" fill="{LABEL}">{shown}</text>')

    if note:
        p.append(f'<text x="14" y="{height-12}" font-size="11" fill="{AXIS}">{note}</text>')
    p.append("</svg>")
    open(out_path, "w").write("\n".join(p))
    print(f"wrote {out_path} ({len(rows)} bars)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--outdir", default="docs")
    a = ap.parse_args()

    seq = collect(a.results, "seq_read", "read_mb_s")
    eff = collect(a.results, "seq_read", "effective_mb_s")
    # The copy baseline's raw read is local disk, so substitute its end-to-end
    # figure; otherwise the chart shows an SSD read next to S3 reads.
    for k, v in eff.items():
        seq[k] = v
    seq = dict(sorted(seq.items(), key=lambda kv: -kv[1]))

    bar_chart(seq, "Sequential read throughput", "MB/s, median of 3 reps, 100 GB file",
              f"{a.outdir}/throughput.svg",
              note="local-nvme shown as end-to-end (download + read), not its local SSD read speed")

    bar_chart(collect(a.results, "ttfb", "first_1mib_ms", lower_is_better=True),
              "Time to first 1 MiB", "milliseconds, lower is better, log scale",
              f"{a.outdir}/latency.svg", log=True,
              note="local-nvme must download the whole 100 GB object before byte 1")

    bar_chart(collect(a.results, "parallel_read", "aggregate_mb_s"),
              "Aggregate throughput, 4 concurrent streams", "MB/s, median of 3 reps",
              f"{a.outdir}/concurrency.svg")

    bar_chart(collect(a.results, "random_seek", "p50_ms", lower_is_better=True),
              "Random seek latency (8 MiB reads)", "p50 milliseconds, lower is better",
              f"{a.outdir}/seek.svg")


if __name__ == "__main__":
    main()
