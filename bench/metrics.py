"""Host-level sampling that runs alongside each measurement.

Throughput alone does not answer "which client should I run". A client that
reaches 2 GB/s while saturating four CPU cores is a different proposition on a
GPU node than one that reaches 1.8 GB/s on half a core, because those cores are
needed for decoding. Likewise a run that silently exhausted its network burst
credits produces a number that will not reproduce in production.
"""

import os
import subprocess
import threading
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")


def drop_caches() -> None:
    """Force the next read to actually come from S3.

    Requires a privileged container: vm.drop_caches is not namespaced, so this
    clears the node's page cache. Without it, every repetition after the first
    would be measuring RAM.
    """
    subprocess.run(["sync"], check=False)
    try:
        with open("/proc/sys/vm/drop_caches", "w") as fh:
            fh.write("3\n")
    except OSError as exc:
        raise RuntimeError(
            "cannot drop the page cache -- results would measure RAM, not S3. "
            "The benchmark pod must run privileged. Underlying error: %s" % exc)


def default_iface() -> str:
    with open("/proc/net/route") as fh:
        for line in fh.readlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1] == "00000000":
                return parts[0]
    return "eth0"


def _iface_rx(iface: str) -> int:
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as fh:
            return int(fh.read().strip())
    except OSError:
        return 0


def ena_allowance_counters(iface: str) -> dict:
    """ENA 'allowance exceeded' counters, which reveal burst-credit exhaustion.

    Only available on the host ENA device, which is why the benchmark pod uses
    host networking. A non-zero delta across a run means the instance hit a
    network limit and the number measured the limit, not the client.
    """
    try:
        out = subprocess.run(["ethtool", "-S", iface], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return {}
    counters = {}
    for line in out.splitlines():
        if "allowance_exceeded" in line and ":" in line:
            k, _, v = line.strip().partition(":")
            try:
                counters[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return counters


class Sampler:
    """Samples CPU/RSS of a client process and NIC bytes at 1 Hz."""

    def __init__(self, pid=None, iface=None, interval=1.0):
        self.pid = pid
        self.iface = iface or default_iface()
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.cpu_samples = []     # instantaneous CPU cores used
        self.rss_samples = []     # bytes
        self.rx_start = 0
        self.rx_end = 0
        self.ena_start = {}
        self.ena_end = {}

    def _proc_cpu_ticks(self):
        try:
            with open(f"/proc/{self.pid}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            # utime and stime are fields 14 and 15 (1-indexed) of the full line
            return int(fields[11]) + int(fields[12])
        except (OSError, IndexError, ValueError):
            return None

    def _proc_rss(self):
        try:
            with open(f"/proc/{self.pid}/statm") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError):
            return None

    def _run(self):
        prev_ticks, prev_t = self._proc_cpu_ticks(), time.monotonic()
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            ticks = self._proc_cpu_ticks()
            if ticks is not None and prev_ticks is not None and now > prev_t:
                self.cpu_samples.append((ticks - prev_ticks) / CLK_TCK / (now - prev_t))
            prev_ticks, prev_t = ticks, now
            rss = self._proc_rss()
            if rss is not None:
                self.rss_samples.append(rss)

    def __enter__(self):
        self.rx_start = _iface_rx(self.iface)
        self.ena_start = ena_allowance_counters(self.iface)
        if self.pid:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.rx_end = _iface_rx(self.iface)
        self.ena_end = ena_allowance_counters(self.iface)
        return False

    def summary(self) -> dict:
        exceeded = {
            k: self.ena_end.get(k, 0) - v
            for k, v in self.ena_start.items()
            if self.ena_end.get(k, 0) - v > 0
        }
        return {
            "cpu_cores_mean": round(sum(self.cpu_samples) / len(self.cpu_samples), 3)
                              if self.cpu_samples else None,
            "cpu_cores_peak": round(max(self.cpu_samples), 3) if self.cpu_samples else None,
            "rss_bytes_peak": max(self.rss_samples) if self.rss_samples else None,
            "nic_rx_bytes": max(0, self.rx_end - self.rx_start),
            "ena_allowance_exceeded": exceeded,
        }
