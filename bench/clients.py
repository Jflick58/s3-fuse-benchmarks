"""Every storage client under test, plus mount/unmount lifecycle.

Each client is defined twice where it has meaningful knobs: once at stock
defaults and once tuned. That split matters because a large part of the answer
to "why is my S3 mount slow" is usually "it is running at defaults", and a
comparison that silently tunes one client but not another is not a comparison.
"""

import dataclasses
import os
import shutil
import signal
import subprocess
import time
from typing import Callable, List, Optional

MOUNT_ROOT = "/mnt/bench"
NVME = "/mnt/nvme"
LUSTRE_PATH = "/mnt/lustre"      # hostPath from the node; corpus lands under /corpus

FUSE = "fuse"
HOSTPATH = "hostpath"
COPY = "copy"


@dataclasses.dataclass
class Client:
    key: str                       # unique id, e.g. "mountpoint-tuned"
    family: str                    # "mountpoint"
    variant: str                   # "default" | "tuned"
    kind: str                      # FUSE | HOSTPATH | COPY
    argv: Optional[Callable] = None
    notes: str = ""

    # populated at runtime
    proc: Optional[subprocess.Popen] = None
    mountpoint: Optional[str] = None
    cache_dir: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.family} ({self.variant})" if self.variant else self.family


def _region_url(region: str) -> str:
    return f"https://s3.{region}.amazonaws.com"


# --------------------------------------------------------------------------
# Client catalogue
# --------------------------------------------------------------------------

def build_catalogue(bucket: str, region: str) -> List[Client]:
    def s3fs(extra):
        def f(mnt, cache):
            return ["s3fs", bucket, mnt, "-f",
                    "-o", "iam_role=auto",
                    "-o", f"endpoint={region}",
                    "-o", f"url={_region_url(region)}"] + extra
        return f

    def mountpoint(extra):
        def f(mnt, cache):
            return ["mount-s3", bucket, mnt,
                    "--region", region, "--foreground", "--read-only"] + extra
        return f

    def geesefs(extra):
        def f(mnt, cache):
            return ["geesefs", "-f", "--region", region] + extra + [bucket, mnt]
        return f

    def rclone(extra):
        remote = f":s3,provider=AWS,region={region},env_auth=true:{bucket}"
        def f(mnt, cache):
            return ["rclone", "mount", remote, mnt, "--read-only"] + extra
        return f

    return [
        Client("s3fs-default", "s3fs", "default", FUSE, s3fs([]),
               "Stock s3fs-fuse. Usually the incumbent, and usually the slowest."),
        Client("s3fs-tuned", "s3fs", "tuned", FUSE, s3fs([
                   "-o", "max_thread_count=32",
                   "-o", "multipart_size=64",
                   "-o", "stat_cache_expire=900",
                   "-o", "max_stat_cache_size=200000",
                   "-o", "enable_negative_cache",
               ]),
               "s3fs with request parallelism raised from the default 10."),

        Client("mountpoint-default", "mountpoint", "default", FUSE, mountpoint([]),
               "AWS's official Rust client, built on the CRT."),
        Client("mountpoint-tuned", "mountpoint", "tuned", FUSE, mountpoint([
                   "--read-part-size", str(16 * 1024 * 1024),
                   "--max-threads", "64",
                   "--metadata-ttl", "indefinite",
               ]),
               "Larger read parts and an immutable-corpus metadata TTL."),

        Client("geesefs-default", "geesefs", "default", FUSE, geesefs([]),
               "Maintained successor to goofys."),
        Client("geesefs-tuned", "geesefs", "tuned", FUSE, geesefs([
                   "--memory-limit", "4096",
                   # GeeseFS expresses read-ahead in KB; the default large-file
                   # window is 100 MB, so this doubles it to 200 MB.
                   "--read-ahead-large", "204800",
                   "--stat-cache-ttl", "15m",
               ]),
               "GeeseFS with a large read-ahead window."),

        Client("rclone-default", "rclone", "default", FUSE, rclone([]),
               "rclone mount with no VFS cache."),
        Client("rclone-tuned", "rclone", "tuned", FUSE, rclone([
                   "--vfs-read-chunk-size", "128M",
                   "--vfs-read-chunk-size-limit", "off",
                   "--vfs-read-ahead", "512M",
                   "--buffer-size", "256M",
                   "--transfers", "16",
                   "--s3-chunk-size", "64M",
                   "--dir-cache-time", "30m",
               ]),
               "rclone with large chunks and aggressive read-ahead."),

        Client("lustre", "FSx for Lustre", "", HOSTPATH, None,
               "S3-linked Lustre, mounted on the node. Measured cold (lazy "
               "import from S3) and warm (already resident) separately."),

        Client("local-nvme", "s5cmd to local NVMe", "", COPY, None,
               "Not a filesystem: copy the object to instance-store NVMe with "
               "parallel ranged GETs, then read locally. The practical upper "
               "bound, and its copy phase doubles as the raw S3 ceiling."),
    ]


def select(catalogue: List[Client], only: Optional[List[str]], lustre_available: bool) -> List[Client]:
    out = []
    for c in catalogue:
        if c.kind == HOSTPATH and not lustre_available:
            continue
        if only and c.key not in only and c.family not in only:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------
# Mount lifecycle
# --------------------------------------------------------------------------

class MountError(RuntimeError):
    pass


def _is_mounted(path: str) -> bool:
    with open("/proc/mounts") as fh:
        return any(f" {path} " in line for line in fh)


def mount(client: Client, timeout: float = 60.0) -> str:
    """Start the client and return the directory its data is visible under."""
    if client.kind == HOSTPATH:
        target = os.path.join(LUSTRE_PATH, "corpus")
        if not os.path.isdir(target):
            raise MountError(f"{LUSTRE_PATH} is not mounted on the node")
        client.mountpoint = target
        return target

    if client.kind == COPY:
        client.mountpoint = os.path.join(NVME, "staged")
        os.makedirs(client.mountpoint, exist_ok=True)
        return client.mountpoint

    mnt = os.path.join(MOUNT_ROOT, client.key)
    cache = os.path.join(NVME, "cache", client.key)
    os.makedirs(mnt, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    client.mountpoint, client.cache_dir = mnt, cache

    argv = client.argv(mnt, cache)
    log = open(f"/tmp/{client.key}.log", "wb")
    # Foreground mode for every FUSE client on purpose: it gives the harness a
    # direct child PID, which is what makes the CPU and RSS attribution in the
    # results trustworthy. A daemonised client would require guessing which
    # process to sample.
    client.proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                   preexec_fn=os.setsid)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.proc.poll() is not None:
            raise MountError(
                f"{client.key} exited with code {client.proc.returncode} before "
                f"mounting. Log:\n{_tail(f'/tmp/{client.key}.log')}")
        if _is_mounted(mnt):
            try:
                os.listdir(mnt)
                return mnt
            except OSError:
                pass
        time.sleep(0.25)

    unmount(client)
    raise MountError(f"{client.key} did not mount within {timeout:.0f}s. Log:\n"
                     f"{_tail(f'/tmp/{client.key}.log')}")


def _tail(path: str, n: int = 25) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return "(no log)"


def unmount(client: Client) -> None:
    if client.kind in (HOSTPATH, COPY):
        return
    mnt = client.mountpoint
    if not mnt:
        return
    for cmd in (["fusermount3", "-u", mnt], ["fusermount", "-u", mnt], ["umount", "-l", mnt]):
        if not _is_mounted(mnt):
            break
        subprocess.run(cmd, capture_output=True)
        time.sleep(0.4)

    if client.proc and client.proc.poll() is None:
        try:
            os.killpg(os.getpgid(client.proc.pid), signal.SIGTERM)
            client.proc.wait(timeout=15)
        except Exception:
            try:
                os.killpg(os.getpgid(client.proc.pid), signal.SIGKILL)
            except Exception:
                pass
    client.proc = None


def clear_client_cache(client: Client) -> None:
    """Wipe the client's own on-disk cache between repetitions.

    Without this, repetition 2 of a client that caches to disk would measure
    local NVMe rather than S3, and that client would appear to beat everything
    else by an order of magnitude for reasons that have nothing to do with S3.
    """
    if client.cache_dir and os.path.isdir(client.cache_dir):
        shutil.rmtree(client.cache_dir, ignore_errors=True)
        os.makedirs(client.cache_dir, exist_ok=True)
    if client.kind == COPY and client.mountpoint:
        shutil.rmtree(client.mountpoint, ignore_errors=True)
        os.makedirs(client.mountpoint, exist_ok=True)
