# S3 filesystem benchmark results

- **Instance**: `m5dn.2xlarge` (8 vCPU), kernel `6.12.100-125.179.amzn2023.x86_64`
- **Region/AZ**: us-west-2 / us-west-2a
- **Corpus**: `dev` profile, primary file `seq/dev-5g-1.bin`
- **Run profile**: `dev`
- **Started**: 2026-08-20T23:32:42Z

## Headline: bulk sequential read

The number that matters for pulling hour-long files onto a node. Rows are ranked on **end-to-end MB/s**, the rightmost throughput column.

For mount-based clients the two throughput columns are the same number. For the copy-based baseline they are not, and the difference matters: its raw read is a **local SSD** read of a file that has already been downloaded, so it is not an S3 measurement at all and must not be compared against a mount. Such rows are marked `(local disk)`, and their end-to-end figure charges both the download and the read.

| client             | raw read MB/s       | end-to-end MB/s | Gbit/s | % of best | CPU cores |
|--------------------|---------------------|-----------------|--------|-----------|-----------|
| mountpoint-tuned   | 1056.8              | 1056.8          | 8.45   | 100%      | 2.7       |
| mountpoint-default | 1024.2              | 1024.2          | 8.19   | 97%       | 2.9       |
| local-nvme         | 6858.6 (local disk) | 958.5           | 7.67   | 91%       | -         |
| geesefs-tuned      | 740.7               | 740.7           | 5.93   | 70%       | 2.6       |
| s3fs-tuned         | 548.1               | 548.1           | 4.38   | 52%       | 5.0       |
| geesefs-default    | 495.2               | 495.2           | 3.96   | 47%       | 1.5       |
| s3fs-default       | 152.9               | 152.9           | 1.22   | 14%       | 3.1       |
| rclone-default     | 99.8                | 99.8            | 0.8    | 9%        | 0.6       |
| rclone-tuned       | 99.6                | 99.6            | 0.8    | 9%        | 0.6       |

Highest throughput observed on this instance: **1325 MB/s (10.6 Gbit/s)**, by `geesefs-tuned` on parallel read. Since the network allowance counters below move during the fastest runs, treat that as the instance's network limit rather than any client's limit.

For contrast, the copy baseline moved S3 to instance-store at **1114 MB/s**. That is not an S3 ceiling: at a 5 GB object it is bounded by whichever of S3, page cache and instance-store write is slowest -- on this class of instance, writes to the instance store usually bind first once the object exceeds RAM. 1 mount configuration beat it, because a mount never writes the object to disk.


## Time to first bytes

Latency to start decoding. A client can win on throughput and still be the wrong choice if nothing is readable for a minute.

| client             | first 1 MiB (ms) | first 64 MiB (ms) |
|--------------------|------------------|-------------------|
| rclone-default     | 30.2             | 695.8             |
| rclone-tuned       | 31.3             | 697.5             |
| geesefs-default    | 37.8             | 409.3             |
| geesefs-tuned      | 38.8             | 427.1             |
| mountpoint-tuned   | 75.2             | 607.6             |
| mountpoint-default | 240.6            | 902.0             |
| s3fs-default       | 802.5            | 834.1             |
| s3fs-tuned         | 2879.8           | 2908.7            |
| local-nvme         | 4843.4           | 4901.9            |

## Random seek (8 MiB reads at random offsets)

| client             | p50 ms | p95 ms | p99 ms |
|--------------------|--------|--------|--------|
| s3fs-tuned         | 2.9    | 1870.0 | 2489.1 |
| local-nvme         | 5.7    | 8.4    | 9.8    |
| geesefs-tuned      | 6.6    | 79.6   | 94.0   |
| geesefs-default    | 78.6   | 173.3  | 186.2  |
| rclone-default     | 158.8  | 260.2  | 328.0  |
| rclone-tuned       | 159.2  | 293.1  | 525.0  |
| mountpoint-default | 184.8  | 389.8  | 642.1  |
| mountpoint-tuned   | 221.7  | 276.4  | 389.9  |
| s3fs-default       | 404.3  | 683.2  | 782.5  |

## Concurrent streams

`spread` is fastest stream / slowest stream. Well above 1.0 means one stream starved the others, which matters when workers share a node.

| client             | aggregate MB/s | aggregate Gbit/s | spread |
|--------------------|----------------|------------------|--------|
| geesefs-tuned      | 1306.2         | 10.45            | 1.2    |
| geesefs-default    | 1276.8         | 10.21            | 1.4    |
| mountpoint-default | 1160.4         | 9.28             | 2.8    |
| mountpoint-tuned   | 1033.3         | 8.27             | 1.6    |
| s3fs-tuned         | 437.6          | 3.5              | 1.3    |
| rclone-default     | 310.2          | 2.48             | 1.2    |
| rclone-tuned       | 262.9          | 2.1              | 1.3    |
| local-nvme         | 159.1          | 1.27             | 8.8    |
| s3fs-default       | 153.5          | 1.23             | 1.2    |

## Metadata (stat/s)

| client             | stat/s   |
|--------------------|----------|
| geesefs-tuned      | 285079.0 |
| mountpoint-tuned   | 283659.8 |
| geesefs-default    | 281338.8 |
| mountpoint-default | 139960.9 |
| s3fs-tuned         | 42188.2  |
| s3fs-default       | 42184.2  |
| rclone-default     | 88.2     |
| rclone-tuned       | 85.2     |

## Validity checks

**Some random-seek measurements were served from page cache, not S3.** These clients moved far fewer bytes over the network than they read, which means the test file fit in node RAM and was prefetched. Their seek latencies below are not S3 latencies. Use the `prod` corpus, whose files are larger than RAM, for trustworthy seek numbers.

| client        | rep | read (MB) | over network (MB) | reported p50 ms |
|---------------|-----|-----------|-------------------|-----------------|
| geesefs-tuned | 0   | 503       | 161               | 6.7             |
| geesefs-tuned | 1   | 503       | 140               | 6.5             |
| local-nvme    | 0   | 503       | 0                 | 6.5             |
| local-nvme    | 1   | 503       | 0                 | 4.8             |

**Network allowance was exceeded during these measurements.** The instance hit an EC2 network limit, so the affected numbers describe the instance, not the client. Re-run on a type with sustained bandwidth before trusting the ranking of the fastest clients.

- `s3fs-default` rep1 random_seek: bw_in_allowance_exceeded +1
- `s3fs-tuned` rep0 seq_read: pps_allowance_exceeded +239
- `s3fs-tuned` rep0 parallel_read: pps_allowance_exceeded +57
- `mountpoint-default` rep0 seq_read: bw_in_allowance_exceeded +728
- `mountpoint-default` rep0 parallel_read: pps_allowance_exceeded +639
- `mountpoint-default` rep1 seq_read: pps_allowance_exceeded +87
- `mountpoint-default` rep1 parallel_read: bw_in_allowance_exceeded +24
- `mountpoint-default` rep1 parallel_read: pps_allowance_exceeded +292
- `mountpoint-tuned` rep0 seq_read: bw_in_allowance_exceeded +435
- `mountpoint-tuned` rep0 seq_read: pps_allowance_exceeded +53
- `mountpoint-tuned` rep0 parallel_read: pps_allowance_exceeded +190
- `mountpoint-tuned` rep1 seq_read: bw_in_allowance_exceeded +863
- `geesefs-default` rep0 seq_read: pps_allowance_exceeded +243
- `geesefs-default` rep0 parallel_read: bw_in_allowance_exceeded +1314
- `geesefs-default` rep0 parallel_read: pps_allowance_exceeded +2297
- `geesefs-default` rep1 parallel_read: bw_in_allowance_exceeded +419
- `geesefs-default` rep1 parallel_read: pps_allowance_exceeded +1634
- `geesefs-tuned` rep0 parallel_read: bw_in_allowance_exceeded +140978
- `geesefs-tuned` rep0 parallel_read: pps_allowance_exceeded +122
- `geesefs-tuned` rep1 seq_read: pps_allowance_exceeded +852
- `geesefs-tuned` rep1 parallel_read: bw_in_allowance_exceeded +207716
- `geesefs-tuned` rep1 parallel_read: pps_allowance_exceeded +5314
- `rclone-default` rep1 random_seek: pps_allowance_exceeded +274
- `local-nvme` rep0 parallel_read: bw_in_allowance_exceeded +858
- `local-nvme` rep0 parallel_read: pps_allowance_exceeded +585
- `local-nvme` rep1 seq_read: pps_allowance_exceeded +44
- `local-nvme` rep1 parallel_read: bw_in_allowance_exceeded +130
- `local-nvme` rep1 parallel_read: pps_allowance_exceeded +4868
