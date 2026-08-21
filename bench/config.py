"""Corpus and run-profile definitions shared by the corpus builder and harness."""

GIB = 1024 ** 3
MIB = 1024 ** 2

# Two corpus profiles. `dev` exists to prove the harness works in minutes for a
# few cents; `prod` uses sizes that match real acquisition codecs, where an hour
# of footage is 50-200 GB (ProRes 422 HQ 1080p is roughly 100 GB/hr).
#
# Size is not cosmetic here. A 1 GiB file is smaller than the page cache on the
# node, so a second read would be served from RAM and every client would look
# identical. The prod files are deliberately larger than RAM.
CORPUS_PROFILES = {
    "dev": {
        "seq": [("dev-1g-%d.bin" % i, 1 * GIB) for i in range(4)]
             + [("dev-5g-%d.bin" % i, 5 * GIB) for i in range(2)],
        "small_count": 2000,
        "small_size": 64 * 1024,
    },
    "prod": {
        "seq": [("prod-10g-%d.bin" % i, 10 * GIB) for i in range(2)]
             + [("prod-50g-%d.bin" % i, 50 * GIB) for i in range(2)]
             + [("prod-100g-0.bin", 100 * GIB)],
        "small_count": 5000,
        "small_size": 64 * 1024,
    },
}

SEQ_PREFIX = "seq/"
SMALL_PREFIX = "small/"
MANIFEST_KEY = "manifest.json"

# Run profiles control how much work the harness does, independent of corpus size.
RUN_PROFILES = {
    "dev": {
        "reps": 2,
        "seq_max_bytes": 5 * GIB,
        "parallel_streams": 4,
        "random_reads": 60,
        "random_read_size": 8 * MIB,
        "metadata_ops": 500,
    },
    "prod": {
        "reps": 3,
        # Cap how much of a very large file the sequential workload reads.
        # Steady state is reached within a few GiB, so this is about runtime,
        # not fidelity: the slowest clients move ~100 MB/s, where every extra
        # GiB costs ten seconds per repetition per client. Random-seek validity
        # depends on the FILE being larger than RAM (it is, at 100 GB), not on
        # how much of it the sequential workload reads.
        "seq_max_bytes": 12 * GIB,
        "parallel_streams": 4,
        "random_reads": 200,
        "random_read_size": 8 * MIB,
        "metadata_ops": 2000,
    },
}
