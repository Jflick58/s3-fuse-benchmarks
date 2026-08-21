# S3 filesystem benchmarks for large video files

Measures how fast an AWS Kubernetes node can pull large files out of S3, across
the filesystem options you would realistically put in front of a GPU video
pipeline. Built for the case where a single asset is 50-200 GB of high-bitrate
footage, not for many small objects.

## What it compares

| Candidate | What it is |
|---|---|
| `s3fs-fuse` | The common incumbent. C++/libcurl. Benchmarked at defaults **and** tuned. |
| `Mountpoint for Amazon S3` | AWS's official client, Rust on the CRT. Defaults and tuned. |
| `GeeseFS` | Maintained successor to goofys. Defaults and tuned. |
| `rclone mount` | Widely deployed, many VFS knobs. Defaults and tuned. |
| `FSx for Lustre` | S3-linked POSIX filesystem. Optional; costs extra. |
| `s5cmd` to local NVMe | Not a filesystem: parallel ranged GETs to instance store, then read locally. |

Each client with meaningful knobs is measured twice, at stock defaults and
tuned. That split is deliberate: a large share of "my S3 mount is slow" turns
out to be "my S3 mount is running at defaults", and that is worth separating
from a genuine difference between projects.

**goofys is intentionally excluded.** Its last release was April 2020; GeeseFS
is its actively maintained successor and supersedes it.

The local-NVMe baseline is not a competitor so much as a yardstick. Its copy
phase is close to the raw achievable S3 throughput for the instance, so every
mount can be read as a percentage of what the hardware can actually do.

## What it measures

| Workload | Question it answers |
|---|---|
| `seq_read` | Bulk sequential throughput. The headline number for hour-long files. |
| `ttfb` | Time to first 1 MiB and first 64 MiB — how soon decoding can start. |
| `random_seek` | p50/p95/p99 for 8 MiB reads at random offsets: scrubbing and keyframe seeks. |
| `parallel_read` | Aggregate throughput with several concurrent streams, plus fairness between them. |
| `metadata` | `stat`/`listdir` rates. Rarely the bottleneck for big files, but often why a mount *feels* broken. |

Alongside every measurement the harness samples the client process's CPU and
RSS, and the node's ENA counters. Both matter: a client that hits 2 GB/s while
burning four cores is a different proposition on a GPU node than one that hits
1.8 GB/s on half a core.

## Quick start

```bash
make data                       # S3 bucket for the corpus (separate TF state)
make up                         # VPC + EKS + one node, then build/push the runner image
make corpus CORPUS_PROFILE=dev  # ~14 GB of test data, a few minutes
make bench   RUN_PROFILE=dev    # run it
make report                     # results/report.md
make down                       # destroy the cluster, keep the corpus
```

Then scale up to realistic sizes:

```bash
make corpus CORPUS_PROFILE=prod   # ~220 GB: 100/50/10 GB files
make bench  RUN_PROFILE=prod
```

Narrow to specific clients while iterating:

```bash
make bench CLIENTS=mountpoint,s3fs
```

## How long a run takes

| Profile | Corpus | Runtime | Cost |
|---|---|---|---|
| `dev` | ~14 GB | 20-35 min | ~$0.30 |
| `prod` | ~220 GB | 1.5-2.5 hr | ~$1.60 |

Most of the `prod` runtime is spent on the slow clients: `s3fs-default` reading
20 GiB three times takes far longer than everything else combined. Once you
know the shape of the results, narrow the field:

```bash
make bench RUN_PROFILE=prod CLIENTS=mountpoint-tuned,geesefs-tuned,local-nvme
```

## Cost

| Component | $/hr |
|---|---|
| `m5dn.2xlarge` node | 0.544 |
| EKS control plane | 0.100 |
| FSx for Lustre, 1200 GiB scratch (optional) | 0.230 |

A full run is roughly 45 minutes, so **about $0.50**, or $0.66 with Lustre.
Same-region S3-to-EC2 transfer is free and the GETs for a 220 GB corpus cost
around a cent. Idle corpus storage is ~$5/month for the `prod` profile.

The dominant cost risk is forgetting to tear down, which is why `make down` is
a first-class target and the corpus lives in its own Terraform state so tearing
down is cheap to do often.

## Design decisions that affect the numbers

**One image, all clients.** Every candidate runs from the same container on the
same node, so distro and library differences cannot masquerade as I/O
differences.

**Page cache is dropped between every repetition.** Otherwise repetition 2 would
measure RAM. This is why the benchmark pod runs privileged. Each client's own
on-disk cache is wiped too, so a caching client cannot quietly benchmark local
NVMe and call it S3.

**Host networking.** Exposes the real ENA device so the harness can read
`bw_in_allowance_exceeded`, and keeps the CNI datapath out of a storage
measurement. If your production pods use normal pod networking and you want
that included, flip `hostNetwork` in `k8s/bench-job.yaml.tpl`.

**S3 gateway VPC endpoint, no NAT gateway.** A NAT gateway would sit directly in
the read path, bill per GB, and impose its own behaviour on the thing being
measured.

**Medians, not bests.** The best run of a usually-slow client is not what
production sees.

**Instance type is the most consequential variable.** For large sequential reads
the network pipe is usually the binding constraint. Types advertised as "Up to
N Gigabit" are burstable: credits deplete partway through a long read and the
benchmark quietly becomes a measurement of burst-credit accounting. The report
has a **Validity checks** section that flags this from the ENA counters — read
it before trusting any ranking among the fastest clients.

The default `m5dn.2xlarge` is burstable. It is the cheapest type with both
instance-store NVMe and a 25 Gbps ceiling, and it is fine for establishing the
shape of the results. For final numbers, move to a sustained-bandwidth type:

```bash
make up NODE_TYPE=m5dn.8xlarge   # 25 Gbps sustained, needs a 32 vCPU quota
```

## Known constraints in this account

These were hit for real while building this, not anticipated on paper.

- **Standard EC2 vCPU quota is 8**, capping the node at 8 vCPU. Increases to 64
  (Standard) and 32 (G/VT) were requested and are still `CASE_OPENED`. Until
  they land, sustained-bandwidth instance types are unavailable, and the
  measured S3 ceiling on `m5dn.2xlarge` is about **1114 MB/s (8.9 Gbit/s)**.

  This matters for interpretation: Mountpoint already reaches ~95% of that
  ceiling, and `bw_in_allowance_exceeded` fires during its runs. On this node
  the fastest clients are limited by the instance, not by the filesystem, so
  the gaps among them are compressed and should not be read as a ranking. The
  slower clients (s3fs, rclone) are nowhere near the ceiling, so their numbers
  are genuine.

- **Node group updates deadlock under that quota.** An in-place managed node
  group update launches the replacement node before draining the old one, which
  needs double the vCPUs. With one 8 vCPU node against an 8 vCPU quota it fails
  with `VcpuLimitExceeded` and eventually `NodeCreationFailure`. The node group
  name therefore hashes its bootstrap config so that changes destroy-then-create.

- **Terraform runs as the account root user.** Hence `API_AND_CONFIG_MAP` auth:
  EKS access entries reject a root principal, and `sts:AssumeRole` is refused
  outright for root ("Roles may not be assumed by root accounts"), so a
  provider-level `assume_role` workaround is not available either. Creating a
  dedicated IAM user for this work would be better practice and would allow the
  modern `API` auth mode.

## Layout

```
terraform/data/     S3 corpus bucket. Separate state so 'make down' cannot delete it.
terraform/cluster/  VPC, EKS, node group, ECR, optional FSx for Lustre.
image/              Dockerfile with every client under test.
bench/              The harness: clients, workloads, metrics, corpus builder, report.
k8s/                Job templates for corpus generation and benchmark runs.
results/            JSONL output and rendered reports.
```

## Enabling FSx for Lustre

```bash
make up FSX=1     # or: terraform apply -var enable_fsx_lustre=true
```

Adds about 10 minutes to apply time. Files import lazily from S3 on first
access, so the harness labels each measurement `cold` or `warm` — reporting
only the warm number would flatter Lustre considerably.
