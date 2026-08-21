"""Benchmark orchestrator.

Run order is deliberate: every client is smoke-tested before any measurement
begins, so a mistyped flag or a missing permission costs ten seconds rather
than surfacing an hour into a run as a suspiciously slow result.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import urllib.request

import clients as C
import workloads as W
from config import RUN_PROFILES
from corpus import load_manifest
from metrics import default_iface, drop_caches, ena_allowance_counters


class Ctx:
    def __init__(self, bucket, region, params, manifest, mount_dir=None):
        self.bucket = bucket
        self.region = region
        self.params = params
        self.manifest = manifest
        self.mount_dir = mount_dir


def imds(path):
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(req, timeout=2).read().decode()
        req = urllib.request.Request(
            f"http://169.254.169.254/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=2).read().decode()
    except Exception:
        return None


def _ver(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return "unavailable"


def run_metadata(args, manifest):
    iface = default_iface()
    return {
        "record": "run_meta",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bucket": args.bucket,
        "region": args.region,
        "run_profile": args.profile,
        "corpus_profile": manifest.get("profile"),
        "instance_type": imds("instance-type"),
        "availability_zone": imds("placement/availability-zone"),
        "kernel": platform.release(),
        "cpu_count": os.cpu_count(),
        # Needed to interpret the copy baseline: an object smaller than RAM is
        # absorbed by page cache, so its "copy" rate is S3-to-memory and does
        # not survive to files larger than memory.
        "mem_total_bytes": _mem_total(),
        "iface": iface,
        # Recorded because they change results and are easy to forget about.
        "sysctl": {
            k: _read_sysctl(k) for k in
            ("net.core.rmem_max", "net.ipv4.tcp_rmem", "net.core.wmem_max")
        },
        "versions": {
            "s3fs": _ver(["s3fs", "--version"]),
            "mountpoint": _ver(["mount-s3", "--version"]),
            "geesefs": _ver(["geesefs", "--version"]),
            "rclone": _ver(["rclone", "version"]),
            "s5cmd": _ver(["s5cmd", "version"]),
        },
        "ena_counters_at_start": ena_allowance_counters(iface),
    }


def _mem_total():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _read_sysctl(name):
    path = "/proc/sys/" + name.replace(".", "/")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def smoke_test(client, ctx, key):
    """Mount, read a little, unmount. Proves flags, IAM and DNS all work."""
    t0 = time.monotonic()
    mnt = C.mount(client)
    try:
        path, _, _ = W.materialize(client, ctx.bucket, key, mnt)
        with open(path, "rb", buffering=0) as fh:
            if len(fh.read(1024 * 1024)) < 1024 * 1024:
                raise RuntimeError("short read during smoke test")
    finally:
        C.unmount(client)
        C.clear_client_cache(client)
    return round(time.monotonic() - t0, 1)


def main():
    ap = argparse.ArgumentParser(description="S3 filesystem benchmark")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--profile", choices=sorted(RUN_PROFILES), default="dev")
    ap.add_argument("--clients", default="",
                    help="comma-separated client keys or families; default is all")
    ap.add_argument("--out", default="/results/results.jsonl")
    ap.add_argument("--skip-smoke", action="store_true")
    ap.add_argument("--run-id", default=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                    help="results are uploaded to s3://<bucket>/results/<run-id>/")
    args = ap.parse_args()

    params = RUN_PROFILES[args.profile]
    manifest = load_manifest(args.bucket, args.region)
    seq = sorted(manifest["seq"], key=lambda f: f["size"])
    if not seq:
        sys.exit("corpus manifest lists no sequential files")

    # Largest file for the throughput/latency workloads: big files are the whole
    # point, and small ones fit in page cache and would flatter every client.
    primary = seq[-1]["key"]
    parallel_keys = [f["key"] for f in seq[-params["parallel_streams"]:]] or [primary]

    lustre_ok = os.path.isdir(os.path.join(C.LUSTRE_PATH, "corpus"))
    only = [s.strip() for s in args.clients.split(",") if s.strip()]
    catalogue = C.build_catalogue(args.bucket, args.region)
    selected = C.select(catalogue, only, lustre_ok)
    if not selected:
        sys.exit("no clients selected")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out = open(args.out, "w")

    def emit(rec):
        out.write(json.dumps(rec) + "\n")
        out.flush()

    meta = run_metadata(args, manifest)
    meta["clients"] = [c.key for c in selected]
    meta["lustre_available"] = lustre_ok
    meta["primary_file"] = primary
    emit(meta)
    print(json.dumps({k: meta[k] for k in
                      ("instance_type", "kernel", "run_profile", "corpus_profile")},
                     indent=2), flush=True)
    if not lustre_ok:
        print("NOTE: FSx for Lustre not mounted on this node; skipping that client.",
              flush=True)

    ctx = Ctx(args.bucket, args.region, params, manifest)

    # ---- preflight -------------------------------------------------------
    if not args.skip_smoke:
        print("\n== preflight ==", flush=True)
        alive = []
        for c in selected:
            try:
                took = smoke_test(c, ctx, seq[0]["key"])
                print(f"  ok    {c.key:22s} ({took}s)", flush=True)
                alive.append(c)
            except Exception as exc:
                print(f"  FAIL  {c.key:22s} {exc}", flush=True)
                emit({"record": "preflight_failure", "client": c.key,
                      "error": str(exc)[:4000]})
        if not alive:
            sys.exit("every client failed preflight; aborting")
        selected = alive

    # ---- measurement -----------------------------------------------------
    touched = {}
    for c in selected:
        print(f"\n== {c.key} ==", flush=True)
        for rep in range(params["reps"]):
            seen = touched.setdefault(c.key, set())
            state = "warm" if primary in seen else "cold"
            seen.add(primary)

            rec = {"record": "measurement", "client": c.key, "family": c.family,
                   "variant": c.variant, "kind": c.kind, "rep": rep,
                   "cache_state": state, "file": primary}
            try:
                ctx.mount_dir = C.mount(c)
                for name, fn in (
                    ("seq_read",      lambda: W.seq_read(c, ctx, primary)),
                    ("ttfb",          lambda: W.ttfb(c, ctx, primary)),
                    ("random_seek",   lambda: W.random_seek(c, ctx, primary)),
                    ("parallel_read", lambda: W.parallel_read(c, ctx, parallel_keys)),
                    ("metadata",      lambda: W.metadata(c, ctx)),
                ):
                    t0 = time.monotonic()
                    try:
                        rec[name] = fn()
                        print(f"  rep{rep} {name:14s} "
                              f"{_headline(name, rec[name])}  "
                              f"[{time.monotonic()-t0:.0f}s]", flush=True)
                    except Exception as exc:
                        rec[name] = {"error": str(exc)[:2000]}
                        print(f"  rep{rep} {name:14s} ERROR {exc}", flush=True)
            except Exception as exc:
                rec["error"] = str(exc)[:4000]
                rec["traceback"] = traceback.format_exc()[:4000]
                print(f"  rep{rep} mount failed: {exc}", flush=True)
            finally:
                C.unmount(c)
                C.clear_client_cache(c)
            emit(rec)

    out.close()
    print(f"\nWrote {args.out}", flush=True)

    # Upload before the pod disappears. Pod logs are not a durable artifact and
    # `kubectl cp` needs a still-running pod, so S3 is the only reliable handoff.
    try:
        import boto3
        key = f"results/{args.run_id}/results.jsonl"
        boto3.client("s3", region_name=args.region).upload_file(
            args.out, args.bucket, key)
        print(f"Uploaded s3://{args.bucket}/{key}", flush=True)
    except Exception as exc:
        print(f"WARNING: could not upload results: {exc}", flush=True)


def _headline(name, res):
    if not isinstance(res, dict):
        return ""
    if "error" in res:
        return "error"
    if "skipped" in res:
        return "skipped"
    for k in ("read_mb_s", "first_1mib_ms", "p50_ms", "aggregate_mb_s", "stat_per_second"):
        if res.get(k) is not None:
            return f"{k}={res[k]}"
    return ""


if __name__ == "__main__":
    sys.exit(main())
