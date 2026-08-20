"""Builds the synthetic test corpus directly in S3, from inside the VPC.

Generating in-region matters: pushing 220 GB from a laptop would take hours and
measure a home uplink. This runs as a Kubernetes Job on the benchmark node, so
the data never leaves AWS.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time

import boto3
from botocore.config import Config

from config import CORPUS_PROFILES, SEQ_PREFIX, SMALL_PREFIX, MANIFEST_KEY

PART = 64 * 1024 * 1024
_local = threading.local()


def _base_block():
    """One high-entropy block per worker thread, reused across parts.

    Each part is stamped with a unique token before upload, so no two parts are
    byte-identical, but the bulk of the buffer is generated once. Drawing 220 GB
    of fresh randomness would make corpus creation CPU-bound on the RNG rather
    than network-bound, for no benefit: S3 does not compress or deduplicate, so
    what matters is only that the bytes are incompressible.
    """
    if not hasattr(_local, "block"):
        _local.block = bytearray(os.urandom(PART))
    return _local.block


def _stamped_part(key, part_number, size):
    block = _base_block()
    token = hashlib.sha256(f"{key}:{part_number}".encode()).digest()
    block[0:32] = token
    block[size // 2:size // 2 + 32] = token
    return bytes(memoryview(block)[:size])


def upload_one(s3, bucket, key, size, quiet=False):
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] == size:
            if not quiet:
                print(f"  = {key} ({size/1e9:.1f} GB) already present", flush=True)
            return
    except s3.exceptions.ClientError:
        pass

    n_parts = (size + PART - 1) // PART
    up = s3.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = up["UploadId"]
    print(f"  + {key} ({size/1e9:.1f} GB, {n_parts} parts)", flush=True)
    t0 = time.monotonic()

    def send(i):
        this = min(PART, size - i * PART)
        resp = s3.upload_part(Bucket=bucket, Key=key, PartNumber=i + 1,
                              UploadId=upload_id, Body=_stamped_part(key, i, this))
        return {"PartNumber": i + 1, "ETag": resp["ETag"]}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            parts = list(pool.map(send, range(n_parts)))
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])})
    except Exception:
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise

    dt = time.monotonic() - t0
    print(f"    done in {dt:.0f}s ({size/dt/1e6:.0f} MB/s)", flush=True)


def build(bucket, region, profile):
    spec = CORPUS_PROFILES[profile]
    cfg = Config(region_name=region, max_pool_connections=64,
                 retries={"max_attempts": 10, "mode": "adaptive"})
    s3 = boto3.client("s3", config=cfg)

    print(f"Building '{profile}' corpus in s3://{bucket}", flush=True)
    seq_keys = []
    for name, size in spec["seq"]:
        key = SEQ_PREFIX + name
        upload_one(s3, bucket, key, size)
        seq_keys.append({"key": key, "size": size})

    n, small_size = spec["small_count"], spec["small_size"]
    print(f"  + {n} small objects ({small_size} B each)", flush=True)
    blob = os.urandom(small_size)

    def put(i):
        s3.put_object(Bucket=bucket, Key=f"{SMALL_PREFIX}obj-{i:06d}.bin", Body=blob)

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        list(pool.map(put, range(n)))

    manifest = {
        "profile": profile,
        "bucket": bucket,
        "region": region,
        "seq": seq_keys,
        "small_prefix": SMALL_PREFIX,
        "small_count": n,
        "small_size": small_size,
        "part_size": PART,
    }
    s3.put_object(Bucket=bucket, Key=MANIFEST_KEY,
                  Body=json.dumps(manifest, indent=2).encode())
    total = sum(f["size"] for f in seq_keys) + n * small_size
    print(f"Corpus ready: {total/1e9:.1f} GB across "
          f"{len(seq_keys)} large + {n} small objects", flush=True)
    return manifest


def load_manifest(bucket, region):
    s3 = boto3.client("s3", region_name=region)
    body = s3.get_object(Bucket=bucket, Key=MANIFEST_KEY)["Body"].read()
    return json.loads(body)


def main():
    ap = argparse.ArgumentParser(description="Build the S3 benchmark corpus")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--profile", choices=sorted(CORPUS_PROFILES), default="dev")
    args = ap.parse_args()
    build(args.bucket, args.region, args.profile)


if __name__ == "__main__":
    sys.exit(main())
