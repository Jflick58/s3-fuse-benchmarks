# S3 filesystem benchmarks

Which way of getting large video files out of S3 onto a Kubernetes node is actually fastest.
Built for 50–200 GB assets (an hour of ProRes 422 HQ is ~100 GB), not for lots of small objects.
Terraform + EKS + a benchmark harness that runs every candidate on the same node.

## Read this first

**Mountpoint for Amazon S3, at defaults, plus `--metadata-ttl indefinite`.** That's the answer.
1320 MB/s vs 127 MB/s for stock s3fs — **10x**. Don't tune its read path; my tuned settings were
*slower* (1214 MB/s). The metadata flag is separate and worth 4600x on `stat`.

- **`results/prod.md`** — the real numbers. 100 GB files, 3 reps, 10 candidates. Read this one.
- **`results/dev.md`** — 5 GB files. Kept as a cautionary tale: at that size everything fits in
  RAM and the results lie (see Gotchas).
- **`docs/*.svg`** — the charts below, regenerate with `bench/chart.py`.

Two exceptions to "just use Mountpoint":
- **Several workers per node ->** GeeseFS. Same aggregate (1362 vs 1320 MB/s) but fair across
  streams (spread 1.1 vs 2.3). Mountpoint lets one stream starve the others.
- **Re-reading the same footage ->** FSx for Lustre. 7.4 ms to first byte and 18 ms seeks, ~18x
  better than anything else. But first touch lazily imports from S3 and is brutal (one cold
  parallel read: 741 s at 17 MB/s), and it's $0.23/hr on top.

Drop s3fs and rclone. rclone does 66 MB/s and 80 `stat`/s.

## Results

100 GB file, `m5dn.2xlarge`, us-west-2, median of 3 reps.

![throughput](docs/throughput.svg)

![concurrency](docs/concurrency.svg)

![latency](docs/latency.svg)

![seek](docs/seek.svg)

| client | seq MB/s | TTFB ms | 4-stream MB/s | spread | seek p50 ms | stat/s |
|---|---|---|---|---|---|---|
| mountpoint-default | **1320** | 163 | 1320 | 2.3 | 328 | 62 |
| mountpoint-tuned | 1214 | 120 | 1321 | 1.8 | 404 | **286,161** |
| geesefs-tuned | 645 | 91 | 1341 | 1.1 | 240 | 286,536 |
| geesefs-default | 431 | 84 | **1362** | **1.1** | 251 | 284,155 |
| lustre | 335 | **7.4** | 315 | 1.2 | **18** | 2,900 |
| local-nvme | 184 | 389,502 | — | — | 17 | — |
| s3fs-tuned | 171 | 3108 | 136 | 1.0 | 377 | 39,782 |
| s3fs-default | 127 | 905 | 127 | 1.0 | 688 | 42,629 |
| rclone-tuned | 100 | 144 | 370 | 1.1 | 261 | 80 |
| rclone-default | 66 | 140 | 380 | 1.0 | 266 | 82 |

**Read the ceiling before you read the ranking.** The top three all converge at 1320–1362 MB/s
(~10.6 Gbit/s) because that's this instance's network limit, and `bw_in_allowance_exceeded` fires
during those runs. Gaps *among the fast clients* are compressed and are not a real ranking. The
slow ones are nowhere near the ceiling, so those numbers stand. To separate the top three you need
a sustained-bandwidth instance:

```bash
make up NODE_TYPE=m5dn.8xlarge   # 25 Gbps sustained -- needs the 32 vCPU quota
```

## Reproduce

```bash
make data                        # S3 bucket, separate TF state so teardown can't nuke it
make up                          # VPC + EKS + 1 node -> builds and pushes the runner image
make corpus CORPUS_PROFILE=prod  # ~236 GB of synthetic files, generated in-region (~11 min)
make bench  RUN_PROFILE=prod     # the run itself (~3 hr) -> uploads results.jsonl to S3
make report                      # results/*.jsonl -> results/*.md
make down                        # kills the cluster, keeps the bucket
```

`CORPUS_PROFILE=dev` / `RUN_PROFILE=dev` gives ~15 GB and ~30 min for validating changes.
`CLIENTS=mountpoint,geesefs` narrows the field. `FSX=1` adds Lustre.

## What it measures

| workload | question |
|---|---|
| `seq_read` | bulk throughput — the headline for hour-long files |
| `ttfb` | ms to first 1 MiB and first 64 MiB — how soon decoding can start |
| `random_seek` | p50/p95/p99 on 8 MiB reads at random offsets — scrubbing, keyframes |
| `parallel_read` | aggregate across 4 streams + fairness between them |
| `metadata` | `stat`/`listdir` rates |

Each measurement also samples the client process's CPU and RSS, and the node's ENA counters. CPU
matters: a client hitting 1320 MB/s on 3.6 cores is a different proposition on a GPU node than one
hitting 645 on 2.2.

## Gotchas (all of these actually bit)

- **Small test files lie.** On 5 GB files the corpus fits in 32 GB of RAM. s3fs "tuning" looked
  like 3.6x; on 100 GB files it's 1.3x. local-nvme looked like 958 MB/s; on real files it's 184
  because you hit NVMe *write* bandwidth (~250 MB/s), not S3. The harness now flags reads served
  from page cache by comparing bytes read against NIC bytes.
- **Don't compare local-nvme's raw read to a mount.** 6.8 GB/s is an SSD read of a file that
  already landed. Its honest number is end-to-end; its `copy_mb_s` is the real S3 ceiling.
- **GeeseFS is a Yandex fork.** Defaults to `storage.yandexcloud.net`, 403s against AWS, falls
  back to the SigV2 signer. Needs `--endpoint`.
- **s3fs renamed its options** in 1.97 — `parallel_count` -> `max_thread_count`,
  `enable_noobj_cache` -> `enable_negative_cache`. Old tuning guides silently do nothing.
- **EKS node group updates deadlock under a tight vCPU quota.** An in-place update launches the
  replacement before draining, so it wants 2x the vCPUs. With one 8 vCPU node against an 8 vCPU
  quota it fails `VcpuLimitExceeded` -> `NodeCreationFailure`. The node group name hashes its
  bootstrap config so changes destroy-then-create instead.
- **Lustre DRA needs `batch_import_meta_data_on_create = true`**, or a corpus written before the
  association exists is invisible under the mount and Lustre silently drops out of the comparison.
- **`kubectl logs -f` won't survive a 3 hr run.** It drops with `http2: client connection lost`,
  which used to abort `make bench` and skip result collection while the Job kept running fine.

## Files

- `terraform/data/` — the corpus bucket. Separate state on purpose: `make down` can't delete it.
- `terraform/cluster/` — VPC (S3 gateway endpoint, no NAT), EKS, node group, ECR, optional FSx.
- `image/Dockerfile` — every client in one image, so distro differences can't masquerade as I/O.
- `bench/clients.py` — mount/unmount + the exact flags per candidate, default and tuned.
- `bench/workloads.py` — the five measurements.
- `bench/metrics.py` — CPU/RSS sampling, page-cache dropping, ENA allowance counters.
- `bench/harness.py` — orchestrator. Smoke-tests every client before measuring anything.
- `bench/report.py`, `bench/chart.py` — jsonl -> markdown, jsonl -> svg.
- `k8s/*.tpl` — corpus and benchmark Jobs.

## Cost

$0.644/hr running (node $0.544 + EKS $0.100), +$0.23/hr with Lustre. The full prod run above cost
about $2.60. Same-region S3 -> EC2 transfer is free; GETs for a 236 GB corpus are about a cent.

Idle cost after `make down` is just the corpus in S3, ~$5/mo for the prod profile.
`make nuke` deletes that too.

## Method notes

- Page cache is dropped between every repetition, and each client's own disk cache is wiped.
  Otherwise rep 2 measures RAM. This is why the pod runs privileged.
- Every client is smoke-tested before any measurement, so a bad flag costs 10 s instead of
  surfacing an hour in as a suspiciously slow result.
- Host networking, to expose the real ENA device for the allowance counters and keep the CNI
  datapath out of a storage measurement.
- Medians, not bests. The best run of a usually-slow client isn't what production sees.
- Clients with meaningful knobs are measured twice, default and tuned, because "your mount is slow"
  is often just "your mount is at defaults" — and that's worth separating from a real difference
  between projects.

## Caveats

- Everything above is one instance type in one region. The ranking among the fast clients is
  network-bound, see above.
- `local-nvme` is measured as copy-then-read. A **pipelined** design (fetch file N+1 while the GPU
  works on N) would hide the copy latency and do much better — the harness doesn't model that, and
  its 4-way concurrent copy is self-inflicted contention a real pipeline wouldn't have.
- `local-nvme` only completed 1 of 3 reps before teardown; its row is a single sample.
- goofys is deliberately excluded. Last release April 2020; GeeseFS supersedes it.
