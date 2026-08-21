# S3 filesystem benchmark results

- **Instance**: `m5dn.2xlarge` (8 vCPU), kernel `6.12.100-125.179.amzn2023.x86_64`
- **Region/AZ**: us-west-2 / us-west-2a
- **Corpus**: `prod` profile, primary file `seq/prod-100g-0.bin`
- **Run profile**: `prod`
- **Started**: 2026-08-21T00:50:53Z

## Headline: bulk sequential read

The number that matters for pulling hour-long files onto a node. Rows are ranked on **end-to-end MB/s**, the rightmost throughput column.

For mount-based clients the two throughput columns are the same number. For the copy-based baseline they are not, and the difference matters: its raw read is a **local SSD** read of a file that has already been downloaded, so it is not an S3 measurement at all and must not be compared against a mount. Such rows are marked `(local disk)`, and their end-to-end figure charges both the download and the read.

| client             | raw read MB/s      | end-to-end MB/s | Gbit/s | % of best | CPU cores |
|--------------------|--------------------|-----------------|--------|-----------|-----------|
| mountpoint-default | 1320.0             | 1320.0          | 10.56  | 100%      | 3.6       |
| mountpoint-tuned   | 1213.9             | 1213.9          | 9.71   | 92%       | 3.2       |
| geesefs-tuned      | 644.9              | 644.9           | 5.16   | 49%       | 2.2       |
| geesefs-default    | 430.8              | 430.8           | 3.45   | 33%       | 1.3       |
| lustre             | 334.7              | 334.7           | 2.68   | 25%       | -         |
| local-nvme         | 552.8 (local disk) | 184.0           | 1.47   | 14%       | -         |
| s3fs-tuned         | 170.9              | 170.9           | 1.37   | 13%       | 1.9       |
| s3fs-default       | 127.3              | 127.3           | 1.02   | 10%       | 2.4       |
| rclone-tuned       | 99.5               | 99.5            | 0.8    | 8%        | 0.6       |
| rclone-default     | 66.1               | 66.1            | 0.53   | 5%        | 0.4       |

Raw parallel-GET ceiling measured on this instance: **276 MB/s (2.2 Gbit/s)**. Treat that as what the hardware can do; a mount's gap to it is the cost of the filesystem layer.

## Time to first bytes

Latency to start decoding. A client can win on throughput and still be the wrong choice if nothing is readable for a minute.

| client             | first 1 MiB (ms) | first 64 MiB (ms) |
|--------------------|------------------|-------------------|
| lustre             | 7.4              | 151.7             |
| geesefs-default    | 84.1             | 733.1             |
| geesefs-tuned      | 90.9             | 562.9             |
| mountpoint-tuned   | 119.7            | 969.2             |
| rclone-default     | 139.9            | 751.8             |
| rclone-tuned       | 143.8            | 793.3             |
| mountpoint-default | 162.8            | 848.4             |
| s3fs-default       | 904.8            | 933.9             |
| s3fs-tuned         | 3107.7           | 3134.8            |
| local-nvme         | 389501.6         | 389536.6          |

## Random seek (8 MiB reads at random offsets)

| client             | p50 ms | p95 ms  | p99 ms  |
|--------------------|--------|---------|---------|
| local-nvme         | 16.5   | 16.5    | 16.6    |
| lustre             | 18.1   | 26.2    | 27.9    |
| geesefs-tuned      | 240.1  | 436.7   | 711.7   |
| geesefs-default    | 251.1  | 469.7   | 602.4   |
| rclone-tuned       | 260.7  | 509.7   | 744.9   |
| rclone-default     | 266.0  | 498.5   | 674.2   |
| mountpoint-default | 327.7  | 529.0   | 740.2   |
| s3fs-tuned         | 377.4  | 16657.6 | 18453.9 |
| mountpoint-tuned   | 404.2  | 634.2   | 714.4   |
| s3fs-default       | 687.9  | 1241.6  | 1662.8  |

## Concurrent streams

`spread` is fastest stream / slowest stream. Well above 1.0 means one stream starved the others, which matters when workers share a node.

| client             | aggregate MB/s | aggregate Gbit/s | spread |
|--------------------|----------------|------------------|--------|
| geesefs-default    | 1361.9         | 10.9             | 1.1    |
| geesefs-tuned      | 1340.9         | 10.73            | 1.1    |
| mountpoint-tuned   | 1321.1         | 10.57            | 1.8    |
| mountpoint-default | 1320.0         | 10.56            | 2.3    |
| rclone-default     | 380.4          | 3.04             | 1.0    |
| rclone-tuned       | 369.6          | 2.96             | 1.1    |
| lustre             | 315.1          | 2.52             | 1.2    |
| s3fs-tuned         | 135.5          | 1.08             | 1.0    |
| s3fs-default       | 127.2          | 1.02             | 1.0    |
| local-nvme         | 15.3           | 0.12             | 5.4    |

## Metadata (stat/s)

| client             | stat/s   |
|--------------------|----------|
| geesefs-tuned      | 286535.9 |
| mountpoint-tuned   | 286160.8 |
| geesefs-default    | 284155.1 |
| s3fs-default       | 42628.5  |
| s3fs-tuned         | 39782.4  |
| lustre             | 2899.7   |
| rclone-default     | 81.7     |
| rclone-tuned       | 79.7     |
| mountpoint-default | 61.7     |

## Validity checks

**Some random-seek measurements were served from page cache, not S3.** These clients moved far fewer bytes over the network than they read, which means the test file fit in node RAM and was prefetched. Their seek latencies below are not S3 latencies. Use the `prod` corpus, whose files are larger than RAM, for trustworthy seek numbers.

| client     | rep | read (MB) | over network (MB) | reported p50 ms |
|------------|-----|-----------|-------------------|-----------------|
| local-nvme | 0   | 1678      | 0                 | 16.5            |

**Network allowance was exceeded during these measurements.** The instance hit an EC2 network limit, so the affected numbers describe the instance, not the client. Re-run on a type with sustained bandwidth before trusting the ranking of the fastest clients.

- `s3fs-default` rep0 random_seek: pps_allowance_exceeded +183
- `s3fs-default` rep1 seq_read: pps_allowance_exceeded +12
- `s3fs-default` rep1 parallel_read: pps_allowance_exceeded +48
- `s3fs-tuned` rep0 parallel_read: pps_allowance_exceeded +563
- `s3fs-tuned` rep1 random_seek: pps_allowance_exceeded +313
- `s3fs-tuned` rep2 parallel_read: pps_allowance_exceeded +829
- `mountpoint-default` rep0 seq_read: bw_in_allowance_exceeded +12700
- `mountpoint-default` rep0 seq_read: pps_allowance_exceeded +881
- `mountpoint-default` rep0 parallel_read: bw_in_allowance_exceeded +1165
- `mountpoint-default` rep0 parallel_read: pps_allowance_exceeded +1933
- `mountpoint-default` rep1 seq_read: bw_in_allowance_exceeded +45006
- `mountpoint-default` rep1 seq_read: pps_allowance_exceeded +4263
- `mountpoint-default` rep1 parallel_read: bw_in_allowance_exceeded +1326
- `mountpoint-default` rep1 parallel_read: pps_allowance_exceeded +2071
- `mountpoint-default` rep2 seq_read: bw_in_allowance_exceeded +40835
- `mountpoint-default` rep2 seq_read: pps_allowance_exceeded +3088
- `mountpoint-default` rep2 parallel_read: bw_in_allowance_exceeded +9
- `mountpoint-default` rep2 parallel_read: pps_allowance_exceeded +707
- `mountpoint-tuned` rep0 seq_read: bw_in_allowance_exceeded +24
- `mountpoint-tuned` rep0 seq_read: pps_allowance_exceeded +461
- `mountpoint-tuned` rep0 parallel_read: bw_in_allowance_exceeded +4540
- `mountpoint-tuned` rep0 parallel_read: pps_allowance_exceeded +979
- `mountpoint-tuned` rep1 seq_read: bw_in_allowance_exceeded +5881
- `mountpoint-tuned` rep1 seq_read: pps_allowance_exceeded +2034
- `mountpoint-tuned` rep2 seq_read: bw_in_allowance_exceeded +465
- `mountpoint-tuned` rep2 seq_read: pps_allowance_exceeded +3124
- `mountpoint-tuned` rep2 parallel_read: bw_in_allowance_exceeded +12
- `mountpoint-tuned` rep2 parallel_read: pps_allowance_exceeded +199
- `geesefs-default` rep0 parallel_read: pps_allowance_exceeded +6406
- `geesefs-default` rep1 seq_read: pps_allowance_exceeded +654
- `geesefs-default` rep1 parallel_read: bw_in_allowance_exceeded +17485
- `geesefs-default` rep1 parallel_read: pps_allowance_exceeded +3637
- `geesefs-default` rep2 parallel_read: bw_in_allowance_exceeded +24
- `geesefs-default` rep2 parallel_read: pps_allowance_exceeded +8016
- `geesefs-tuned` rep0 seq_read: pps_allowance_exceeded +1325
- `geesefs-tuned` rep0 parallel_read: bw_in_allowance_exceeded +79052
- `geesefs-tuned` rep0 parallel_read: pps_allowance_exceeded +11735
- `geesefs-tuned` rep1 seq_read: pps_allowance_exceeded +841
- `geesefs-tuned` rep1 parallel_read: bw_in_allowance_exceeded +216714
- `geesefs-tuned` rep1 parallel_read: pps_allowance_exceeded +565
