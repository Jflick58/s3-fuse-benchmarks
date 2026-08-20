# Every S3 filesystem under test lives in this one image, so all candidates run
# against an identical kernel, libc, CPU allocation and credential path. Running
# each client from its own upstream image would fold distro and library
# differences into results that are supposed to be about I/O.
FROM ubuntu:24.04

ARG S3FS_VERSION=v1.97
ARG GEESEFS_VERSION=v0.43.8
ARG S5CMD_VERSION=2.3.0
ARG RCLONE_VERSION=current

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip jq git \
      build-essential autoconf automake pkg-config libtool \
      libcurl4-openssl-dev libxml2-dev libssl-dev \
      libfuse-dev libfuse3-dev fuse3 \
      python3 python3-boto3 \
      fio ffmpeg \
      procps iproute2 ethtool util-linux coreutils \
      xfsprogs \
    && rm -rf /var/lib/apt/lists/*

# --- s3fs-fuse ---------------------------------------------------------------
# Built from source rather than taken from the Ubuntu archive: 24.04 ships
# 1.93, and several tuning options were renamed after that (parallel_count ->
# max_thread_count, enable_noobj_cache -> enable_negative_cache). Pinning the
# current release keeps the flags in bench/clients.py meaningful and gives
# s3fs its best showing rather than a handicapped one.
RUN git clone --depth 1 --branch ${S3FS_VERSION} https://github.com/s3fs-fuse/s3fs-fuse.git /tmp/s3fs \
    && cd /tmp/s3fs \
    && ./autogen.sh && ./configure --prefix=/usr && make -j"$(nproc)" && make install \
    && rm -rf /tmp/s3fs

# --- Mountpoint for Amazon S3 ------------------------------------------------
RUN curl -fsSL -o /tmp/mount-s3.deb https://s3.amazonaws.com/mountpoint-s3-release/latest/x86_64/mount-s3.deb \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/mount-s3.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/mount-s3.deb

# --- GeeseFS -----------------------------------------------------------------
RUN curl -fsSL -o /usr/local/bin/geesefs \
      https://github.com/yandex-cloud/geesefs/releases/download/${GEESEFS_VERSION}/geesefs-linux-amd64 \
    && chmod +x /usr/local/bin/geesefs

# --- rclone ------------------------------------------------------------------
RUN curl -fsSL -o /tmp/rclone.zip https://downloads.rclone.org/rclone-${RCLONE_VERSION}-linux-amd64.zip \
    && unzip -q -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin \
    && chmod +x /usr/local/bin/rclone && rm /tmp/rclone.zip

# --- s5cmd (drives the local-NVMe prefetch baseline) -------------------------
RUN curl -fsSL -o /tmp/s5cmd.tgz \
      https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-64bit.tar.gz \
    && tar -xzf /tmp/s5cmd.tgz -C /usr/local/bin s5cmd && rm /tmp/s5cmd.tgz

# Ubuntu 24.04 will not install fuse (v2 utils) and fuse3 side by side, so the
# image standardises on fuse3. s3fs still builds against the libfuse2 headers,
# and the Go-based clients shell out to a binary called `fusermount`, so point
# that name at fusermount3.
RUN ln -sf /usr/bin/fusermount3 /usr/bin/fusermount

# allow_other is required so the harness can read a mount created by a
# different uid inside the container.
RUN echo "user_allow_other" >> /etc/fuse.conf

# Freeze each tool's help text into the image. The harness diffs its configured
# flags against these at startup, so a client that renamed an option fails in
# seconds with a clear message instead of silently mounting with defaults and
# producing a plausible but wrong number.
RUN mkdir -p /opt/help \
    && (s3fs --help        > /opt/help/s3fs.txt 2>&1 || true) \
    && (mount-s3 --help    > /opt/help/mountpoint.txt 2>&1 || true) \
    && (geesefs --help     > /opt/help/geesefs.txt 2>&1 || true) \
    && (rclone mount --help> /opt/help/rclone.txt 2>&1 || true) \
    && (s5cmd --help       > /opt/help/s5cmd.txt 2>&1 || true)

COPY bench /opt/bench
ENV PYTHONUNBUFFERED=1
WORKDIR /opt/bench
ENTRYPOINT ["/usr/bin/python3"]
