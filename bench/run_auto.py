#!/usr/bin/env python3
from argparse import ArgumentParser
import asyncio
from collections.abc import Sequence
from pathlib import Path
import shlex
import subprocess
from subprocess import CalledProcessError
from asyncio import sleep
import csv
import sys
import os
import time
import datetime
import threading
import matplotlib.pyplot as plt

import tempfile
import re
from pathlib import Path
import subprocess
import signal
import xml.etree.ElementTree as ET

from psutil import Process
from qemu.qmp import QMPClient

from scripts.config import BALLOON_CFG, DEFAULT_DISK, ModeAction, DEFAULT_SWAP, DEFAULT_SWAP_SIZE_GB
from scripts.qemu import qemu_vm, qemu_wait_startup
from scripts.utils import SSHExec, fmt_bytes, non_block_read, rm_ansi_escape, setup
from scripts.vm_resize import VMResize

DEFAULT_LIBVIRT_NETWORK = "default"
DEFAULT_APP_CGROUP_PATH = "/sys/fs/cgroup/blowfish-auto"


def modes_requiring_vfio(mode: str) -> bool:
    """VFIO passthrough is required for every mode except blowfish-auto."""
    return mode not in ("blowfish-auto",)


def _cgroup_leaf_usable_without_sudo(cgroup_path: str) -> bool:
    """True if leaf cgroup v2 files exist and the current euid can drive host bench cgroup ops."""
    base = Path(cgroup_path)
    if not base.is_dir():
        return False
    for name in ("cgroup.procs", "memory.high", "memory.max"):
        f = base / name
        if not f.exists() or not os.access(f, os.W_OK):
            return False
    pressure = base / "memory.pressure"
    if pressure.exists() and not os.access(pressure, os.R_OK):
        return False
    return True

_BENCH_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_ROOT.parent

# Provisioned into the guest home via scp before swap/code warmup (must exist on this machine).
GUEST_SETUP_ARTIFACTS: tuple[tuple[Path, str], ...] = (
    (_BENCH_ROOT / "vm/vm_scipts/swap_on.sh", "~/swap_on.sh"),
    (_BENCH_ROOT / "vm/code_writer/code_write", "~/code_write"),
    (_BENCH_ROOT / "vm/code_writer/code_manage", "~/code_manage"),
    (_BENCH_ROOT / "vm/code_writer/run_code.sh", "~/run_code.sh"),
)


def ensure_guest_setup_artifacts_present() -> None:
    """Raise FileNotFoundError if any vm/* provisioning file is missing locally."""
    missing = [str(p) for p, _ in GUEST_SETUP_ARTIFACTS if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing guest setup files (build code_writer / code_manage or fix paths):\n  "
            + "\n  ".join(missing)
        )


async def upload_guest_setup_artifacts(ssh: SSHExec) -> None:
    """Upload swap/code binaries and helper scripts to the guest home directory."""
    ensure_guest_setup_artifacts_present()
    for local_path, remote_spec in GUEST_SETUP_ARTIFACTS:
        await ssh.upload(local_path, remote_spec)
    await ssh.run("chmod +x ~/swap_on.sh ~/code_write ~/code_manage ~/run_code.sh")


# Preset guest paths for --app (mutually exclusive with --app-path)
APP_SCRIPT_PRESETS = {
    "tc": "/home/debian/code_tc/test_tc.sh",
}


def resolve_guest_app_script(args) -> str:
    """
    Guest script path from --app-path (explicit) or --app (preset).
    If neither is given, defaults to the ``tc`` preset.
    """
    if getattr(args, "app_path", None):
        return args.app_path
    name = getattr(args, "app", None)
    if name and name in APP_SCRIPT_PRESETS:
        return APP_SCRIPT_PRESETS[name]
    return APP_SCRIPT_PRESETS["tc"]


def _artifact_time_suffix(args) -> str:
    """Return YYYYMMDD_HHMMSS when ``--time`` is set; otherwise empty."""
    if not getattr(args, "time", False):
        return ""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def extract_tc_summary_metrics(args, app_stdout: str) -> tuple[str, str, str]:
    """
    Return ``(read_time, relabel, trial_time)`` strings for ``summary.csv``.

    * ``tc``: parse ``Read Time:`` / ``Relabel:`` / ``Trial Time:``.
    """
    z = ("", "", "")
    if getattr(args, "app_path", None):
        return z
    app = getattr(args, "app", "tc")
    if app != "tc":
        return z

    def grab(pattern_label: str) -> str:
        m = re.search(
            rf"^\s*{re.escape(pattern_label)}\s+([\d.]+)\s*$",
            app_stdout,
            re.MULTILINE,
        )
        return m.group(1) if m else ""

    return (
        grab("Read Time:"),
        grab("Relabel:"),
        grab("Trial Time:"),
    )


def ensure_host_benchmark_cgroup(cgroup_path: str) -> None:
    """
    Ensure the leaf cgroup directory exists on the host (cgroup v2), once per bench run.

    If ``cgroup_path`` already exists and is writable by the current user (typical after a prior
    ``sudo mkdir`` + ``chown`` delegation), skip creation. Otherwise ``sudo mkdir -p`` and
    ``sudo chown -R`` to the invoking euid so later RSSRecorder host writes avoid ``sudo``.

    Per-test PID/memory reset happens in RSSRecorder._setup_cgroup_limits.
    """
    if not cgroup_path:
        return
    if _cgroup_leaf_usable_without_sudo(cgroup_path):
        return
    cg = shlex.quote(cgroup_path)
    uid, gid = os.geteuid(), os.getegid()
    subprocess.check_output(
        ["bash", "-lc", f"sudo mkdir -p {cg} && sudo chown -R {uid}:{gid} {cg}"],
        text=True,
    )


# Class for recording RSS
class RSSRecorder:
    def __init__(
        self,
        process,
        output_file,
        ssh=None,
        max_bytes=None,
        psi_target=None,
        debug=False,
        cgroup_path="/sys/fs/cgroup/blowfish-auto",
        reclaim_step_pages_grow=50000,
        reclaim_step_pages_shrink=50000,
    ):
        self.process = process
        self.output_file = output_file
        self.ssh = ssh  # SSH client for querying guest state and driving reclaim
        self.max_bytes = max_bytes  # Guest RAM cap (bytes)
        self.psi_target = psi_target  # Target PSI (0-100)
        self.debug = debug
        self.running = False
        self.thread = None
        # blowfish-auto: read PSI from the host cgroup QEMU is joined to and drive guest
        # reclaim via /sys/kernel/reclaim/pagenums (cgroup memory.high is not used).
        self.cgroup_path = cgroup_path
        self.reclaim_step_pages_grow = max(1, int(reclaim_step_pages_grow))
        self.reclaim_step_pages_shrink = max(1, int(reclaim_step_pages_shrink))
        self.timing_log_path = Path(self.output_file).with_name(
            f"{Path(self.output_file).stem}_timing.log"
        )
        self.step_timing_stats = {}
        # Host cgroup: set after successful _setup_cgroup_limits join
        self._host_cgroup_setup_done = False

    def _run_host_cmd(self, command):
        result = subprocess.check_output(["bash", "-lc", command], text=True)
        return result.strip()

    def _host_write_file(self, path: str, content: str) -> None:
        """Write one line to a cgroup sysfs file; use direct write if permitted, else ``sudo tee``."""
        line = str(content).rstrip("\n") + "\n"
        p = Path(path)
        if p.exists() and os.access(p, os.W_OK):
            try:
                p.write_text(line)
                return
            except OSError:
                pass
        subprocess.run(
            ["sudo", "tee", str(p)],
            input=line.encode(),
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def _collect_pressure_totals_from_output(self, pressure_output):
        """Parse memory PSI `some` line totals only (ignore `full`)."""
        stats = {'memory': {'some': 0}}
        if not pressure_output:
            return stats
        for line in pressure_output.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':')
            if len(parts) < 2:
                continue
            path = parts[0]
            content = ":".join(parts[1:])
            comp = path.split('/')[-1].split('.')[0]
            if comp != 'memory':
                continue
            content_parts = content.split()
            if not content_parts:
                continue
            pres_type = content_parts[0]
            if pres_type != 'some':
                continue
            total_val = 0
            for item in content_parts:
                if item.startswith('total='):
                    try:
                        total_val = int(item.split('=')[1])
                    except Exception:
                        total_val = 0
                    break
            stats['memory']['some'] = total_val
        return stats

    def _build_pressure_grep_cmd(self):
        cgroup_quoted = shlex.quote(self.cgroup_path)
        return f"grep -H . {cgroup_quoted}/memory.pressure"

    def _fetch_psi_pressure(self):
        """Read memory.pressure from the host cgroup QEMU is joined to."""
        return self._run_host_cmd(self._build_pressure_grep_cmd())

    def teardown_host_cgroup(self):
        """
        Reset host leaf cgroup memory limits after the run.

        QEMU is not moved back to the v2 root cgroup (the process exits and is destroyed).
        Leaf directory is kept (created once per bench run).
        """
        if not self.cgroup_path or not self._host_cgroup_setup_done:
            return
        try:
            self._host_write_file(f"{self.cgroup_path}/memory.max", "max")
            self._host_write_file(f"{self.cgroup_path}/memory.high", "max")
        except Exception as e:
            print(f"Warning: could not reset memory limits on host cgroup {self.cgroup_path}: {e}")
        self._host_cgroup_setup_done = False

    async def _setup_cgroup_limits(self):
        if not self.cgroup_path:
            return
        # Leaf is created at bench start; each test resets limits then (re)attaches QEMU.
        # cgroup memory limits stay at "max": the cgroup is only used to observe PSI, while
        # reclaim is driven via /sys/kernel/reclaim/pagenums in the guest.
        self._host_write_file(f"{self.cgroup_path}/memory.max", "max")
        self._host_write_file(f"{self.cgroup_path}/memory.high", "max")
        self._host_write_file(f"{self.cgroup_path}/cgroup.procs", str(self.process.pid))
        self._host_cgroup_setup_done = True
        print(
            "[CGROUP_LIMIT] phase=init blowfish-auto: host cgroup joined for PSI only; "
            "guest reclaim via /sys/kernel/reclaim/pagenums"
        )

    def _signed_delta_pages(self, signal):
        if signal == 0:
            return 0
        if signal > 0:
            # Positive signal ⇒ tighten reclaim pressure: reclaim more pages.
            step_pages = self.reclaim_step_pages_shrink
        else:
            # Negative signal ⇒ relax reclaim pressure: reclaim fewer pages.
            step_pages = self.reclaim_step_pages_grow
        return int(signal * step_pages)

    # 0.1 PSI (0–100 scale) ↔ 10_000 pages/s ⇒ 1.0 PSI ↔ 100_000 pages/s
    _PSI_PGPGIN_PAGES_PER_S_PER_UNIT = 10000.0 / 0.1

    def _throughput_target_pages_per_s(self) -> float:
        if self.psi_target is None:
            return 0.0
        return float(self.psi_target) * self._PSI_PGPGIN_PAGES_PER_S_PER_UNIT

    def _read_pgpgin_total(self) -> int | None:
        """Cumulative pgpgin (swap-in) from the host /proc/vmstat."""
        try:
            for line in Path("/proc/vmstat").read_text().splitlines():
                if line.startswith("pgpgin "):
                    return int(line.split()[1])
        except Exception:
            return None
        return None

    def _combine_psi_pgpgin_control(
        self,
        current_mem_some: float,
        dt: float,
        rate_pgpgin: float | None,
    ) -> int:
        """
        Signed page delta for the pagenums reclaim convention used by this recorder:
        positive ⇒ tighten (more reclaim), negative ⇒ relax (fewer reclaim pages).

        Uses PSI and pgpgin (swap-in) throughput vs a target derived from PSI:
        ``target_rate = psi_target * (10000/0.1)`` pages/s.

        - If **either** observed signal is above its target (PSI or throughput), relax using the
          average of the two relax page deltas when both fire; if only one fires, use that delta.
        - If **both** are strictly below targets, tighten using the average of the two tighten deltas.
        - Otherwise fall back to PSI-only stepping.
        """
        if self.psi_target is None:
            return 0
        if dt <= 0:
            return self._signed_delta_pages(self.psi_target - current_mem_some)
        if rate_pgpgin is None:
            return self._signed_delta_pages(self.psi_target - current_mem_some)

        tgt = self._throughput_target_pages_per_s()
        psi_over = current_mem_some > float(self.psi_target)
        thr_over = rate_pgpgin > tgt
        psi_strict_under = current_mem_some < float(self.psi_target)
        thr_strict_under = rate_pgpgin < tgt

        if psi_over or thr_over:
            diff = float(self.psi_target) - current_mem_some
            psi_relax = abs(self._signed_delta_pages(diff)) if psi_over else 0
            thr_relax = max(0, int((rate_pgpgin - tgt) * dt)) if thr_over else 0
            n_relax = int(psi_over) + int(thr_over)
            if n_relax == 2:
                combined = max(0, int(round((psi_relax + thr_relax) / 2.0)))
            else:
                combined = psi_relax + thr_relax
            return -combined

        if psi_strict_under and thr_strict_under:
            diff = float(self.psi_target) - current_mem_some
            psi_tight = abs(self._signed_delta_pages(diff))
            thr_tight = max(0, int((tgt - rate_pgpgin) * dt))
            combined = max(0, int(round((psi_tight + thr_tight) / 2.0)))
            return combined

        return self._signed_delta_pages(self.psi_target - current_mem_some)

    def _record_step_timing(self, log_file, iteration, step_name, elapsed_s):
        if not self.debug or log_file is None:
            return
        stats = self.step_timing_stats.setdefault(
            step_name, {"count": 0, "total_s": 0.0, "max_s": 0.0}
        )
        stats["count"] += 1
        stats["total_s"] += elapsed_s
        stats["max_s"] = max(stats["max_s"], elapsed_s)
        log_file.write(f"iter={iteration:04d} step={step_name:<20} elapsed={elapsed_s:.6f}s\n")

    def _write_timing_summary(self, log_file):
        if not self.debug or log_file is None:
            return
        log_file.write("\n=== Timing Summary ===\n")
        if not self.step_timing_stats:
            log_file.write("No timing data collected.\n")
            return
        ranked = sorted(
            self.step_timing_stats.items(),
            key=lambda item: item[1]["max_s"],
            reverse=True,
        )
        for step_name, stats in ranked:
            avg_s = stats["total_s"] / stats["count"] if stats["count"] else 0.0
            log_file.write(
                f"{step_name:<20} count={stats['count']:4d} avg={avg_s:.6f}s max={stats['max_s']:.6f}s\n"
            )
        top_step, top_stats = ranked[0]
        log_file.write(
            f"\nHighest max latency step: {top_step} ({top_stats['max_s']:.6f}s)\n"
        )
        
    def start(self):
        if self.running:
            return False
            
        self.running = True
        self.thread = threading.Thread(target=self._record_task)
        self.thread.daemon = True
        self.thread.start()
        return True
        
    def stop(self):
        if not self.running:
            return False
            
        self.running = False
        try:
            if self.thread:
                self.thread.join(timeout=2)
                self.thread = None
                
            tag = self.psi_target if self.psi_target is not None else "unknown"
            psi_csv_path = Path(self.output_file).parent / f"psi_data_{tag}.csv"
            if psi_csv_path.exists():
                try:
                    plot_thread = threading.Thread(
                        target=plot_psi_chart,
                        args=(psi_csv_path, self.psi_target)
                    )
                    plot_thread.start()
                    plot_thread.join()
                except Exception as e:
                    print(f"Error plotting PSI chart: {e}")
        finally:
            self.teardown_host_cgroup()

        return True
        
    async def _get_vm_memory_stats(self):
        """Return (free_bytes, swap_used_bytes, swap_cached_bytes) from the guest."""
        if not self.ssh:
            return None, None, None
        
        try:
            # One line: guest free, swap used, SwapCached (from /proc/meminfo)
            command = """
            echo "$(free -b | grep Mem | awk '{print $4}') $(free -b | grep Swap | awk '{print $3}') $(grep SwapCached /proc/meminfo | awk '{print $2}')"
            """
            result = await self.ssh.output(command)
            
            # Parsed as: "free_bytes swap_used swap_cached_kb"
            if result:
                parts = result.strip().split()
                if len(parts) >= 3:
                    free_bytes = int(parts[0])
                    swap_used = int(parts[1])
                    # SwapCached from meminfo is in kB
                    swap_cached = int(parts[2]) * 1024
                    return free_bytes, swap_used, swap_cached
                    
        except Exception as e:
            print(f"Error getting VM memory stats: {e}")
        
        return None, None, None

    def _record_task(self):
        # PSI samples CSV for optional mem-pressure plot
        tag = self.psi_target if self.psi_target is not None else "unknown"
        psi_csv_path = Path(self.output_file).parent / f"psi_data_{tag}.csv"
        timing_f = self.timing_log_path.open('w') if self.debug else None
        if timing_f:
            timing_f.write(f"Timing log for {Path(self.output_file).name}\n")
            timing_f.write("All durations are wall-clock seconds measured inside RSSRecorder.\n\n")

        try:
            with open(self.output_file, 'w') as f, open(psi_csv_path, 'w') as psi_f:
                psi_f.write("timestamp,mem_some,pgpgin_pages_per_s,pgpgin_target_pages_per_s\n")
                f.write("timestamp,rss_bytes,vm_free_bytes,real_free_bytes\n")

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                last_psi_time = time.time()
                last_psi_stats = {'memory': {'some': 0}}
                last_pgpgin: int | None = None

                if self.ssh and self.max_bytes and self.psi_target is not None:
                    try:
                        loop.run_until_complete(self._setup_cgroup_limits())
                    except Exception as e:
                        print(f"Warning: Failed to setup cgroup controller: {e}")

                try:
                    result = self._fetch_psi_pressure()
                    if result:
                        last_psi_stats = self._collect_pressure_totals_from_output(result)
                except Exception as e:
                    print(f"Error reading initial PSI: {e}")

                last_psi_time = time.time()

                iteration = 0
                while self.running:
                    try:
                        iteration += 1
                        sample_meta: dict = {}

                        step_start = time.perf_counter()
                        time.sleep(0.1)
                        self._record_step_timing(timing_f, iteration, "sleep", time.perf_counter() - step_start)

                        loop_start = time.perf_counter()
                        timestamp = time.time()

                        step_start = time.perf_counter()
                        rss = self.process.memory_info().rss
                        self._record_step_timing(timing_f, iteration, "rss_read", time.perf_counter() - step_start)

                        step_start = time.perf_counter()
                        vm_free, swap_used, swap_cached = loop.run_until_complete(self._get_vm_memory_stats()) if self.ssh else (None, None, None)
                        self._record_step_timing(timing_f, iteration, "vm_memory_stats", time.perf_counter() - step_start)
                        vm_free_str = str(vm_free) if vm_free is not None else ""

                        psi_vals_map = {}

                        if self.cgroup_path:
                            try:
                                step_start = time.perf_counter()
                                psi_output = self._fetch_psi_pressure()
                                self._record_step_timing(timing_f, iteration, "psi_fetch", time.perf_counter() - step_start)

                                psi_time = time.time()
                                psi_delta_time = psi_time - last_psi_time

                                if psi_delta_time > 0 and psi_output:
                                    step_start = time.perf_counter()
                                    current_psi = self._collect_pressure_totals_from_output(psi_output)
                                    self._record_step_timing(timing_f, iteration, "psi_parse", time.perf_counter() - step_start)

                                    delta = current_psi['memory']['some'] - last_psi_stats['memory']['some']
                                    val = 0 if delta < 0 else (delta / (psi_delta_time * 1000000.0)) * 100
                                    psi_vals_map['mem_some'] = min(100.0, val)

                                    step_start = time.perf_counter()
                                    pgpgin_now = self._read_pgpgin_total()
                                    self._record_step_timing(
                                        timing_f, iteration, "pgpgin_read", time.perf_counter() - step_start
                                    )
                                    current_pgpgin_rate: float | None = None
                                    if (
                                        pgpgin_now is not None
                                        and last_pgpgin is not None
                                        and psi_delta_time > 0
                                    ):
                                        current_pgpgin_rate = (pgpgin_now - last_pgpgin) / psi_delta_time
                                    if pgpgin_now is not None:
                                        last_pgpgin = pgpgin_now

                                    tgt_pg = self._throughput_target_pages_per_s()
                                    rate_str = (
                                        str(int(round(current_pgpgin_rate)))
                                        if current_pgpgin_rate is not None
                                        else ""
                                    )
                                    tgt_str = (
                                        str(int(round(tgt_pg)))
                                        if self.psi_target is not None
                                        else ""
                                    )

                                    step_start = time.perf_counter()
                                    psi_f.write(
                                        f"{timestamp},{psi_vals_map['mem_some']:.2f},{rate_str},{tgt_str}\n"
                                    )
                                    psi_f.flush()
                                    self._record_step_timing(timing_f, iteration, "psi_csv_write", time.perf_counter() - step_start)

                                    sample_meta["psi_dt"] = psi_delta_time
                                    sample_meta["pgpgin_rate"] = current_pgpgin_rate

                                    last_psi_stats = {'memory': current_psi['memory'].copy()}
                                    last_psi_time = psi_time

                            except Exception as e:
                                print(f"Error recording PSI: {e}")

                        real_free = None
                        swapped = None
                        if vm_free is not None and swap_used is not None and swap_cached is not None:
                            swapped = swap_used - swap_cached
                            real_free = max(0, vm_free - swapped)

                        real_free_str = str(real_free) if real_free is not None else ""

                        step_start = time.perf_counter()
                        f.write(f"{timestamp},{rss},{vm_free_str},{real_free_str}\n")
                        f.flush()
                        self._record_step_timing(timing_f, iteration, "rss_csv_write", time.perf_counter() - step_start)

                        if self.ssh and self.max_bytes and self.psi_target is not None:
                            try:
                                if vm_free is not None and self.max_bytes > 0:
                                    free_ratio = vm_free / self.max_bytes
                                    if free_ratio >= 0.8:
                                        self._record_step_timing(timing_f, iteration, "loop_total", time.perf_counter() - loop_start)
                                        continue

                                if self.psi_target is not None:
                                    dt_u = sample_meta.get("psi_dt")
                                    rate_u = sample_meta.get("pgpgin_rate")
                                    current_mem_some = psi_vals_map.get("mem_some", 0)
                                    if dt_u is not None:
                                        signed_pages = self._combine_psi_pgpgin_control(
                                            current_mem_some, float(dt_u), rate_u
                                        )
                                        tgt_pg = self._throughput_target_pages_per_s()
                                        rate_log = (
                                            str(int(round(float(rate_u))))
                                            if rate_u is not None
                                            else "-"
                                        )
                                        pagenums = max(0, int(signed_pages))
                                        try:
                                            step_start = time.perf_counter()
                                            loop.run_until_complete(
                                                self.ssh.run(
                                                    f"echo {pagenums} | sudo tee /sys/kernel/reclaim/pagenums"
                                                )
                                            )
                                            self._record_step_timing(
                                                timing_f,
                                                iteration,
                                                "reclaim_write",
                                                time.perf_counter() - step_start,
                                            )
                                            print(
                                                f"[PSI_PAGENUMS] target={self.psi_target:.3f} "
                                                f"current={current_mem_some:.3f} pagenums={pagenums} "
                                                f"signed_pages={signed_pages} "
                                                f"pgpgin_rate={rate_log} "
                                                f"pgpgin_target={int(round(tgt_pg))}"
                                            )
                                        except Exception as e:
                                            print(f"Error writing reclaim pagenums (psi): {e}")

                            except Exception as e:
                                print(f"Error in auto reclaim logic: {e}")

                        self._record_step_timing(timing_f, iteration, "loop_total", time.perf_counter() - loop_start)

                    except Exception as e:
                        print(f"Error recording memory data: {e}")

                loop.close()
        finally:
            if timing_f:
                self._write_timing_summary(timing_f)
                timing_f.close()


def resolve_libvirt_network(parser: ArgumentParser, net_name: str, net_mac: str | None, guest_ip: str | None) -> dict[str, str | None]:
    """Resolve bridge/gateway/netmask and guest MAC/IP from libvirt network XML.

    Uses the first ``<dhcp><host mac='…' ip='…'/>`` in document order for defaults when
    ``--net-mac`` / ``--guest-ip`` are omitted. If there is no such entry, ``parser.error``.
    """
    try:
        xml = subprocess.check_output(
            ["virsh", "net-dumpxml", net_name], text=True, stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError as e:
        parser.error(f"failed to run `virsh net-dumpxml {net_name}`: {e.output.strip()}")
        raise

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        parser.error(f"failed to parse libvirt network XML: {e}")
        raise

    bridge_name = None
    guest_gateway = None
    guest_netmask = None

    bridge_node = root.find("bridge")
    if bridge_node is not None:
        bridge_name = bridge_node.get("name")

    first_host_mac: str | None = None
    first_host_ip: str | None = None
    for ip_node in root.findall("ip"):
        dhcp = ip_node.find("dhcp")
        if dhcp is None:
            continue
        for host in dhcp.findall("host"):
            hm = (host.get("mac") or "").strip()
            hip = (host.get("ip") or "").strip()
            if hm and hip:
                first_host_mac = hm.lower()
                first_host_ip = hip
                break
        if first_host_mac:
            break

    ip_nodes = root.findall("ip")
    for ip_node in ip_nodes:
        addr = ip_node.get("address")
        netmask = ip_node.get("netmask")
        family = ip_node.get("family")
        if family is None or family == "ipv4":
            if addr and netmask:
                guest_gateway = addr
                guest_netmask = netmask
                break

    if not bridge_name:
        parser.error(f"libvirt network '{net_name}' has no bridge name in XML")
    if not guest_gateway or not guest_netmask:
        parser.error(f"libvirt network '{net_name}' has no ipv4 address/netmask in XML")
    if not first_host_mac or not first_host_ip:
        parser.error(
            f"libvirt network '{net_name}' has no DHCP <host mac='…' ip='…'/> entry in net-dumpxml"
        )

    resolved_net_mac = net_mac or first_host_mac
    resolved_guest_ip = guest_ip or first_host_ip
    if not resolved_net_mac or not resolved_guest_ip:
        parser.error(
            f"libvirt network '{net_name}': could not resolve --net-mac / --guest-ip after reading XML"
        )

    return {
        "bridge_name": bridge_name,
        "guest_gateway": guest_gateway,
        "guest_netmask": guest_netmask,
        "guest_ip": resolved_guest_ip,
        "net_mac": resolved_net_mac,
    }


def compute_time_weighted_average(values, timestamps):
    if not values:
        return 0.0
    if len(values) == 1 or len(timestamps) <= 1:
        return sum(values) / len(values)

    weighted_sum = 0.0
    total_duration = 0.0
    for i in range(len(values) - 1):
        dt = timestamps[i + 1] - timestamps[i]
        if dt <= 0:
            continue
        weighted_sum += values[i] * dt
        total_duration += dt

    if total_duration <= 0:
        return sum(values) / len(values)
    return weighted_sum / total_duration


def compute_reclamation_stats(rss_values, timestamps, max_bytes):
    """Time-weighted reclamation ratio over all samples (no stable-phase split)."""
    if not rss_values or not max_bytes:
        return {
            "avg_rss_bytes": 0.0,
            "avg_reclaimed_bytes": 0.0,
            "reclamation_ratio": 0.0,
        }

    avg_rss_bytes = compute_time_weighted_average(rss_values, timestamps)
    avg_reclaimed_bytes = max_bytes - avg_rss_bytes
    reclamation_ratio = avg_reclaimed_bytes / max_bytes

    return {
        "avg_rss_bytes": avg_rss_bytes,
        "avg_reclaimed_bytes": avg_reclaimed_bytes,
        "reclamation_ratio": reclamation_ratio,
    }


# Draw RSS line chart with reclamation ratios
def plot_rss_chart(file_path, max_bytes=None):
    try:
        timestamps = []
        rss_values = []
        vm_free_values = []
        real_free_values = []
        
        with open(file_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                timestamps.append(float(row['timestamp']))
                rss_values.append(int(row['rss_bytes']))
                
                # Guest free memory column (may be empty)
                if 'vm_free_bytes' in row and row['vm_free_bytes']:
                    vm_free_values.append(int(row['vm_free_bytes']))
                else:
                    vm_free_values.append(None)
                    
                if 'real_free_bytes' in row and row['real_free_bytes']:
                    real_free_values.append(int(row['real_free_bytes']))
                else:
                    real_free_values.append(None)
        
        if not timestamps:
            print("No memory data found to plot")
            return None
            
        # Treat missing samples as zero where we plot series
        valid_vm_free_data = False
        if vm_free_values and any(x is not None for x in vm_free_values):
            valid_vm_free_data = True
            vm_free_values = [0 if x is None else x for x in vm_free_values]
            
        # Real free (after swap accounting), same fill rule
        valid_real_free_data = False
        if real_free_values and any(x is not None for x in real_free_values):
            valid_real_free_data = True
            real_free_values = [0 if x is None else x for x in real_free_values]
            
        # Relative time axis (seconds)
        start_time = timestamps[0]
        rel_timestamps = [t - start_time for t in timestamps]
        
        # Guest memory series in GB
        vm_free_gb = [f / (1024**3) if f is not None else 0 for f in vm_free_values]
        real_free_gb = [f / (1024**3) for f in real_free_values] if valid_real_free_data else []
        
        stats = compute_reclamation_stats(rss_values, timestamps, max_bytes)
        avg_rss_bytes = stats["avg_rss_bytes"] if max_bytes else sum(rss_values) / len(rss_values)
        avg_rss_gb = avg_rss_bytes / (1024**3)

        # Averages for title text
        avg_free_ratio = 0
        avg_real_free_gb = 0
        if valid_vm_free_data:
            avg_vm_free_bytes = sum(vm_free_values) / len(vm_free_values)
            avg_vm_free_gb = avg_vm_free_bytes / (1024**3)
            
            # Free memory as fraction of configured guest RAM
            vm_total_gb = max_bytes / (1024**3) if max_bytes else None
            if vm_total_gb:
                avg_free_ratio = avg_vm_free_bytes / max_bytes
                print(f"Average VM Free Memory: {avg_vm_free_gb:.2f}GB ({avg_free_ratio:.2%} of total)")
                
        if valid_real_free_data:
            avg_real_free_bytes = sum(real_free_values) / len(real_free_values)
            avg_real_free_gb = avg_real_free_bytes / (1024**3)
            print(f"Average Real Free Memory: {avg_real_free_gb:.2f}GB")
        
        # Figure
        fig, ax1 = plt.subplots(figsize=(12, 8))
        
        # Line colors
        rss_color = 'k'
        reclaimable_color = 'g'  # reclaimable (includes swap cache semantics from guest)
        reclaimed_color = 'r'   # host-side reclaimed (max_bytes - RSS)
        free_color = 'b'        # guest "real" free
        
        plot_elements = []
        plot_labels = []
        
        rss_gb = [r / (1024**3) for r in rss_values]
        line_rss = ax1.plot(rel_timestamps, rss_gb, f'{rss_color}-', linewidth=2, label='Host RSS')
        plot_elements.extend(line_rss)
        plot_labels.append('Host RSS')

        # Reclaimable memory (green), if present
        if valid_vm_free_data:
            line_reclaimable = ax1.plot(rel_timestamps, vm_free_gb, f'{reclaimable_color}-', 
                                      linewidth=2, label='Reclaimable Memory')
            
            # Legend entry
            plot_elements.extend(line_reclaimable)
            plot_labels.append('Reclaimable Memory')
            
        # Real free memory (blue), if present
        if valid_real_free_data:
            line_free = ax1.plot(rel_timestamps, real_free_gb, f'{free_color}-', 
                               linewidth=2, label='Free Memory')
            
            # Legend entry
            plot_elements.extend(line_free)
            plot_labels.append('Free Memory')
        
        # Reclaimed memory from host RSS vs guest RAM cap
        if max_bytes is not None:
            reclaimed_gb = [(max_bytes - r) / (1024**3) for r in rss_values]
            line_reclaimed = ax1.plot(rel_timestamps, reclaimed_gb, f'{reclaimed_color}-', 
                                    linewidth=2, label='Reclaimed Memory')
            
            # Legend entry
            plot_elements.extend(line_reclaimed)
            plot_labels.append('Reclaimed Memory')
            
            # Average reclaimed for subtitle
            avg_reclaimed = stats["avg_reclaimed_bytes"] / (1024**3)
            reclamation_ratio = stats["reclamation_ratio"]
            
            # Multi-line title
            title = 'Guest-Internal Memory View Over Time (with Host RSS)\n'
            title += f'Time-weighted Avg Host RSS: {avg_rss_gb:.2f}GB, Reclaimed: {avg_reclaimed:.2f}GB\n'
            title += f'Reclamation ratio: {reclamation_ratio:.2%}'
            
            if valid_vm_free_data:
                title += f', Reclaimable: {avg_vm_free_gb:.2f}GB'
                
            if valid_real_free_data:
                title += f', Free: {avg_real_free_gb:.2f}GB'
        else:
            title = f'Guest-Internal Memory View Over Time (with Host RSS)\nAvg Host RSS: {avg_rss_gb:.2f}GB'
        
        # Axis labels
        ax1.set_xlabel('Time (seconds)')
        ax1.set_ylabel('Memory (GB)')
        
        # Legend
        if plot_elements:
            ax1.legend(plot_elements, plot_labels, loc='best')
        
        # Title and grid
        plt.title(title)
        ax1.grid(True)

        ax1.set_ylim(bottom=0)
        
        # Save PDF
        output_path = file_path.with_suffix('.pdf')
        plt.savefig(output_path, format='pdf', dpi=300)
        
        plt.close(fig)  # free matplotlib figure memory
        
        return output_path
    except Exception as e:
        print(f"Error plotting memory chart: {e}")
        import traceback
        traceback.print_exc()
        return None


def plot_psi_chart(file_path, psi_target=None):
    """Plot memory PSI `some` stall percentage over time."""
    try:
        timestamps = []
        mem_some = []

        with open(file_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                timestamps.append(float(row['timestamp']))
                mem_some.append(float(row['mem_some']))

        if not timestamps:
            print("No PSI data found to plot")
            return None

        start_time = timestamps[0]
        rel_timestamps = [t - start_time for t in timestamps]

        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(rel_timestamps, mem_some, label='memory some', color='blue')
        ax.set_title('Memory PSI (some)')
        ax.set_ylabel('% stalled')
        ax.set_xlabel('Time (s)')
        ax.grid(True)
        ax.legend()

        if psi_target is not None:
            y_max = max(1.0, float(psi_target) + 1.0)
            ax.set_ylim(0, y_max)

        plt.tight_layout()
        output_path = file_path.with_suffix('.pdf')
        plt.savefig(output_path, format='pdf', dpi=300)
        print(f"PSI chart saved to {output_path}")
        plt.close(fig)
        return output_path

    except Exception as e:
        print(f"Error plotting PSI chart: {e}")
        return None



# setup_vm: optional log_dir for cmd.sh, boot.txt, meta.json, etc.
async def setup_vm(qemu_path, kernel_path, args, swap_path, root, log_dir=None):
    print("Starting QEMU VM...")
    print(
        f"Binding guest RAM to host NUMA node {args.host_numa_node} with policy=bind. "
        "Please ensure the pinned host CPUs belong to the same NUMA node."
    )
    # Calculate memory settings
    min_mem = 2
    extra_args = BALLOON_CFG[args.mode](args.cores, args.mem, min_mem, args.mem)
    
    # Add swap device parameters
    extra_args.extend([
        "-drive", 
        f"file={swap_path},if=virtio,index=0,media=disk"
    ])
    
    # Result/log directory (defaults to bench run root)
    if log_dir is None:
        log_dir = root
    
    os.makedirs(log_dir, exist_ok=True)
    
    # Artifact paths under log_dir
    cmd_sh_path = log_dir / "cmd.sh"
    boot_txt_path = log_dir / "boot.txt"
    
    # Start VM without stdout redirection
    qemu = qemu_vm(
        qemu_path,
        args.port,
        kernel_path,
        args.cores,
        hda=args.img,
        qmp_port=args.qmp,
        extra_args=extra_args,
        vfio_device=args.vfio_dev if modes_requiring_vfio(args.mode) else None,
        network_mode=args.net_mode,
        hostfwd_rules=[f"tcp:127.0.0.1:{args.port}-:22"],
        net_mac=args.net_mac,
        bridge_name=args.bridge_name,
        host_numa_node=args.host_numa_node,
        # qemu_vm does not capture QEMU stdout here
    )
    ps_proc = Process(qemu.pid)

    # Persist exact QEMU argv for reproducibility
    cmd_sh_path.write_text(shlex.join(qemu.args))
    await qemu_wait_startup(qemu, boot_txt_path)
    
    meta_data = {
        "date": datetime.datetime.now().isoformat(),
        "memory": f"{args.mem}GB",
        "cores": args.cores,
        "mode": args.mode,
        "qemu": qemu_path,
        "kernel": kernel_path
    }
    
    with open(log_dir / "meta.json", 'w') as f:
        import json
        json.dump(meta_data, f, indent=2)
    
    ssh_host = args.guest_ip if args.net_mode == "libvirt" else "localhost"
    ssh_port = 22 if args.net_mode == "libvirt" else args.port
    ssh = SSHExec(args.user, host=ssh_host, port=ssh_port)
    print(
        f"Waiting for SSH to become ready on {ssh_host}:{ssh_port} "
        f"(timeout={args.ssh_ready_timeout}s)..."
    )
    await wait_for_ssh_ready(
        ssh,
        timeout_s=args.ssh_ready_timeout,
        initial_interval_s=args.ssh_ready_interval,
    )

    await upload_guest_setup_artifacts(ssh)

    print("Configuring swap device...")
    await ssh.run("bash -lc '$HOME/swap_on.sh'")

    await sleep(1)

    # Configure transparent huge pages
    # print("Setting transparent huge pages to madvise...")
    # await ssh.run("echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled")
    # await ssh.run("echo 0 | sudo tee /sys/kernel/mm/transparent_hugepage/khugepaged/defrag")
    await ssh.run("cat /sys/kernel/mm/transparent_hugepage/enabled")
    await sleep(1)

    print("Preparing for application execution...")
    code_process = await ssh.process(
        f"bash -lc '~/run_code.sh {args.mem - 1}'"
    )
    await sleep(1)
    await ssh.run("bash -lc '$HOME/code_manage -w'")
    await sleep(1)
    await ssh.run("bash -lc '$HOME/code_manage -e'")
    await code_process.wait()
    code_process = None
    print("Preparing for application execution done...")

    # Connect to QMP
    qmp = QMPClient("STREAM machine")
    await qmp.connect(("127.0.0.1", args.qmp))

    return qemu, qmp, ssh, ps_proc


async def wait_for_ssh_ready(ssh, timeout_s=180, initial_interval_s=1.0, max_interval_s=10.0):
    """
    Wait until SSH becomes responsive.
    Useful right after VM boot when SSH service may not be ready yet.
    """
    start = time.time()
    attempt = 0
    interval = max(0.2, initial_interval_s)
    last_error = None

    while time.time() - start < timeout_s:
        attempt += 1
        try:
            # Keep the probe command tiny and side-effect free.
            await ssh.output("echo SSH_READY")
            elapsed = time.time() - start
            print(f"SSH is ready after {elapsed:.1f}s (attempt {attempt}).")
            return
        except Exception as e:
            last_error = e
            elapsed = time.time() - start
            print(
                f"SSH not ready yet (attempt {attempt}, elapsed {elapsed:.1f}s): {e}. "
                f"Retrying in {interval:.1f}s..."
            )
            await asyncio.sleep(interval)
            interval = min(max_interval_s, interval * 1.5)

    raise TimeoutError(
        f"SSH is still not ready after {timeout_s}s. "
        f"Last error: {last_error}"
    )

async def monitor_dmesg(ssh, output_file, stop_event):
    """
    Poll guest dmesg and append new lines to output_file until stop_event.
    """
    print(f"Starting dmesg monitoring, output to {output_file}...")
    last_line_count = 0
    
    with open(output_file, 'w') as f:
        f.write("=== DMESG Monitoring Started ===\n")
        f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n\n")
        f.flush()
        
        while not stop_event.is_set():
            try:
                dmesg_output = await ssh.output("dmesg")
                
                if dmesg_output:
                    lines = dmesg_output.strip().split('\n')
                    current_line_count = len(lines)
                    
                    # Append only new tail lines
                    if current_line_count > last_line_count:
                        new_lines = lines[last_line_count:]
                        for line in new_lines:
                            f.write(line + '\n')
                        f.flush()
                        last_line_count = current_line_count
                        
                        # Echo SWAP_DEBUG lines to the console
                        debug_lines = [l for l in new_lines if '[SWAP_DEBUG]' in l]
                        for line in debug_lines:
                            print(f"[DMESG] {line}")
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"Error monitoring dmesg: {e}")
                await asyncio.sleep(1)
        
        f.write(f"\n=== DMESG Monitoring Stopped ===\n")
        f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
    
    print(f"Dmesg monitoring stopped, saved to {output_file}")


async def run_app_once(ssh, _args):
    """
    Run the guest workload script once (path from CLI / presets).
    Returns (stdout, duration_seconds).
    """

    app_path = resolve_guest_app_script(_args)
    cgroup_path = getattr(_args, "app_cgroup_path", "/sys/fs/cgroup/blowfish-auto")
    bash_inv = f"bash {shlex.quote(app_path)}"
    app_cmd = bash_inv
    if cgroup_path:
        cgroup_quoted = shlex.quote(cgroup_path)
        inner = (
            "sudo mkdir -p {cg} && "
            "echo $$ | sudo tee {cg}/cgroup.procs >/dev/null && "
            "exec {inv}"
        ).format(cg=cgroup_quoted, inv=bash_inv)
        app_cmd = f"bash -lc {shlex.quote(inner)}"
    start_time = time.time()
    result = await ssh.output(app_cmd)
    duration = time.time() - start_time
    if cgroup_path:
        cg = shlex.quote(cgroup_path)
        await ssh.run(f"sudo rmdir {cg} 2>/dev/null || true")
    return result, duration


# Run application script in VM (single run)
async def run_app_in_vm(ssh, args, result_dir=None, test_info=None):
    """Run the app once and optionally save stdout to result_dir."""
    print("Running application...")

    dmesg_task = None
    stop_event = None
    if args.debug and result_dir:
        stop_event = asyncio.Event()
        dmesg_file = result_dir / f"dmesg_runtime_{test_info}.txt" if test_info else result_dir / "dmesg_runtime.txt"
        dmesg_task = asyncio.create_task(monitor_dmesg(ssh, dmesg_file, stop_event))
        await asyncio.sleep(0.2)

    result, duration = await run_app_once(ssh, args)

    if dmesg_task and stop_event:
        print("Stopping dmesg monitoring...")
        stop_event.set()
        try:
            await asyncio.wait_for(dmesg_task, timeout=5.0)
        except asyncio.TimeoutError:
            print("Warning: dmesg monitoring task did not finish in time")
            dmesg_task.cancel()

    print(f"Application run time: {duration:.2f}s")

    if result_dir:
        info_str = f"_{test_info}" if test_info else ""
        ts = _artifact_time_suffix(args)
        name = f"app_result{info_str}_{ts}.txt" if ts else f"app_result{info_str}.txt"
        result_file = result_dir / name

        with open(result_file, 'w') as f:
            f.write("=== Application Result ===\n\n")
            f.write(result)

        print(f"Saved application result to {result_file}")

    return [result]


async def run_app_only_in_vm(ssh, args, result_dir=None, test_info=None):
    """all-local path: run app once and optionally save stdout."""
    print("Running application...")
    result, duration = await run_app_once(ssh, args)
    print(f"Application run time: {duration:.2f}s")

    if result_dir:
        info_str = f"_{test_info}" if test_info else ""
        ts = _artifact_time_suffix(args)
        name = f"app_result{info_str}_{ts}.txt" if ts else f"app_result{info_str}.txt"
        result_file = result_dir / name
        with open(result_file, "w") as f:
            f.write("=== Application Result ===\n\n")
            f.write(result)
        print(f"Saved application result to {result_file}")

    return [result]


async def run_llfree_4k_app_only_mode(args, root, swap_path):
    """
    If mode is all-local: only boot VM, run app, save app result.
    """
    print("Starting all-local app-only mode...")
    test_dir = root / "all-local"
    test_dir.mkdir(exist_ok=True)

    qemu = None
    qmp = None
    ssh = None

    try:
        qemu, qmp, ssh, _ = await setup_vm(args.qemu, args.kernel, args, swap_path, root, test_dir)
        await sleep(1)
        await run_app_only_in_vm(ssh, args, result_dir=test_dir, test_info="all-local")
    finally:
        if qmp:
            try:
                await qmp.disconnect()
            except Exception as e:
                print(f"Warning: Failed to disconnect QMP: {e}")

        if qemu:
            try:
                qemu.terminate()
            except Exception as e:
                print(f"Warning: Failed to terminate QEMU: {e}")
            await sleep(3)

    print(f"all-local app-only mode completed. Results saved in {test_dir}")

async def main(argv: Sequence[str] | None = None):
    parser = ArgumentParser(
        description="manual test"
    )
    parser.add_argument("--qemu")
    parser.add_argument("--kernel")
    parser.add_argument("--user", default="debian")
    parser.add_argument("--img", default=str(DEFAULT_DISK))
    parser.add_argument("--port", type=int, default=5300)
    parser.add_argument("--qmp", type=int, default=5400)
    parser.add_argument("-m", "--mem", type=int, default=22)
    parser.add_argument("-c", "--cores", type=int, default=20)
    parser.add_argument(
        "--mode", choices=list(BALLOON_CFG.keys()), required=True, action=ModeAction
    )
    parser.add_argument(
        "--vfio-dev",
        type=str,
        metavar="BDF",
        help=(
            "PCI BDF for vfio-pci passthrough (e.g. b1:00.3); device must be bound to vfio-pci. "
            "Required for every mode except blowfish-auto."
        ),
    )
    parser.add_argument(
        "--host-numa-node",
        type=int,
        default=0,
        help="Bind guest base RAM to this host NUMA node with QEMU policy=bind; ensure pinned CPUs are on the same NUMA node",
    )
    app_mx = parser.add_mutually_exclusive_group(required=False)
    app_mx.add_argument(
        "--app",
        choices=sorted(APP_SCRIPT_PRESETS.keys()),
        default="tc",
        metavar="NAME",
        help=(
            "Preset workload inside guest. "
            + ", ".join(f"{k}={v}" for k, v in APP_SCRIPT_PRESETS.items())
        ),
    )
    app_mx.add_argument(
        "--app-path",
        dest="app_path",
        metavar="PATH",
        default=None,
        help="Absolute path to application script inside guest (cannot combine with --app)",
    )
    parser.add_argument(
        "--app-cgroup-path",
        default=DEFAULT_APP_CGROUP_PATH,
        help=(
            "Host cgroup v2 leaf that QEMU is joined to for PSI monitoring (blowfish-auto). "
            "The leaf is created once at bench start; each test resets memory limits to 'max' "
            "and re-attaches QEMU (leaf kept for reuse)."
        ),
    )
    parser.add_argument(
        "--reclaim-step-pages-grow",
        dest="reclaim_step_pages_grow",
        type=int,
        default=50000,
        help="Control step in pages when relaxing guest reclaim (default: 50000)",
    )
    parser.add_argument(
        "--reclaim-step-pages-shrink",
        dest="reclaim_step_pages_shrink",
        type=int,
        default=50000,
        help="Control step in pages when tightening guest reclaim (default: 50000)",
    )
    parser.add_argument(
        "--debug",
        "--DEBUG",
        action="store_true",
        dest="debug",
        help=(
            "Verbose bench diagnostics: stream SWAP_DEBUG dmesg lines during app run (when result_dir is set) "
            "and write RSSRecorder per-step timing logs (*_timing.log)"
        ),
    )
    parser.add_argument(
        "--net-mode",
        choices=["user", "libvirt"],
        default="libvirt",
        help="QEMU network mode: libvirt (default) or user",
    )
    parser.add_argument(
        "--guest-ip",
        type=str,
        default=None,
        help="Guest VM IP for SSH in libvirt mode, e.g. 192.168.122.10",
    )
    parser.add_argument(
        "--guest-gateway",
        type=str,
        default=None,
        help="Guest VM gateway in libvirt mode",
    )
    parser.add_argument(
        "--guest-netmask",
        type=str,
        default=None,
        help="Guest VM netmask in libvirt mode",
    )
    parser.add_argument(
        "--libvirt-net",
        type=str,
        default=DEFAULT_LIBVIRT_NETWORK,
        help="Libvirt network name used to resolve bridge/ip settings via virsh net-dumpxml",
    )
    parser.add_argument(
        "--bridge-name",
        type=str,
        default=None,
        help="Bridge name for libvirt mode; auto-resolved from virsh net-dumpxml when omitted",
    )
    parser.add_argument(
        "--net-mac",
        type=str,
        default=None,
        help="Optional MAC address for virtio-net in libvirt mode",
    )
    parser.add_argument(
        "--libvirt-domain",
        type=str,
        default="test",
        help="Unused; MAC/IP for libvirt come from the first DHCP <host/> in virsh net-dumpxml (kept for CLI compatibility)",
    )
    parser.add_argument(
        "--ssh-ready-timeout",
        type=int,
        default=180,
        help="Seconds to wait for SSH to become ready after VM boot",
    )
    parser.add_argument(
        "--ssh-ready-interval",
        type=float,
        default=1.0,
        help="Initial retry interval (seconds) while waiting for SSH readiness",
    )
    parser.add_argument(
        "--psi",
        nargs="+",
        type=float,
        default=None,
        metavar="TARGET",
        help=(
            "Explicit PSI target value(s) on [0, 100]. "
            "If set, only these targets are run (in order) instead of the built-in sweep."
        ),
    )
    args, root = setup(parser, argv)

    # Resolved guest script for logs / meta clarity (default preset: tc)
    args.resolved_app_script = resolve_guest_app_script(args)
    print(f"Guest application script: {args.resolved_app_script}")
    try:
        import json
        meta_path = root / "meta.json"
        if meta_path.is_file():
            with meta_path.open() as f:
                meta = json.load(f)
            meta["resolved_app_script"] = args.resolved_app_script
            with meta_path.open("w") as f:
                json.dump(meta, f, indent=2)
    except Exception as e:
        print(f"Warning: could not update meta.json with resolved app path: {e}")

    if args.ssh_ready_timeout <= 0:
        parser.error("--ssh-ready-timeout must be > 0")
    if args.ssh_ready_interval <= 0:
        parser.error("--ssh-ready-interval must be > 0")

    if args.psi is not None:
        for p in args.psi:
            if p < 0.0 or p > 100.0:
                parser.error(f"--psi values must be between 0 and 100, got {p}")

    if modes_requiring_vfio(args.mode):
        if not args.vfio_dev or not str(args.vfio_dev).strip():
            parser.error(
                f"mode {args.mode!r} requires --vfio-dev "
                "(blowfish-auto does not use VFIO passthrough)"
            )

    try:
        ensure_guest_setup_artifacts_present()
    except FileNotFoundError as err:
        parser.error(str(err))

    if args.net_mode == "libvirt":
        resolved = resolve_libvirt_network(parser, args.libvirt_net, args.net_mac, args.guest_ip)
        if not args.bridge_name:
            args.bridge_name = resolved["bridge_name"]
        if not args.guest_gateway:
            args.guest_gateway = resolved["guest_gateway"]
        if not args.guest_netmask:
            args.guest_netmask = resolved["guest_netmask"]
        if not args.guest_ip:
            args.guest_ip = resolved["guest_ip"]
        if not args.net_mac:
            args.net_mac = resolved["net_mac"]

    # Check if swap device exists, create it if not
    swap_path = Path(DEFAULT_SWAP)
    print(f"Swap path: {swap_path}")
    if not swap_path.exists():
        print(f"Creating swap device at {swap_path} with size {DEFAULT_SWAP_SIZE_GB}G")
        swap_path.parent.mkdir(parents=True, exist_ok=True)
        # Create file with specified size
        subprocess.run([
            "dd", 
            f"if=/dev/zero", 
            f"of={swap_path}", 
            f"bs=1G", 
            f"count={DEFAULT_SWAP_SIZE_GB}"
        ], check=True)

    # Common variables
    max_bytes = args.mem * 1024**3
    # -------------- all-local APP-ONLY MODE --------------
    if args.mode == "all-local":
        await run_llfree_4k_app_only_mode(args, root, swap_path)
        return

    # -------------- AUTO TRAVERSE MODE --------------
    await run_auto_traverse_mode(args, root, swap_path, max_bytes)

async def run_auto_traverse_mode(args, root, swap_path, max_bytes):
    """Run in automatic traversal mode (PSI targets only)."""
    print("Starting automatic traversal mode (PSI targets)...")

    tests_dir = root

    # Summary file for results
    summary_file = tests_dir / "summary.csv"
    with open(summary_file, 'w') as f:
        f.write("psi_target,reclamation_ratio,read_time,relabel,trial_time,chart_path\n")

    if args.app_cgroup_path:
        try:
            ensure_host_benchmark_cgroup(args.app_cgroup_path)
            print(f"[CGROUP] host leaf ready (mkdir once): {args.app_cgroup_path}")
        except Exception as e:
            print(f"Warning: could not create host cgroup path {args.app_cgroup_path}: {e}")
    
    if getattr(args, "psi", None):
        target_values = list(args.psi)
        print(f"Using explicit PSI targets from --psi: {target_values}")
    else:
        # Default scripted sweep (same grid as previous --strategy psi)
        target_values = [i / 100.0 for i in range(100, 101, 100)]
        print(f"Using default PSI sweep: {target_values}")

    # Traverse targets
    for target_val in target_values:
        psi_target = target_val

        label = f"psi_{target_val:.1f}"
        print_label = f"PSI target: {target_val}"

        print(f"\n{'='*80}")
        print(f"Testing with {print_label}")
        print(f"{'='*80}\n")
        
        qemu = None
        qmp = None
        rss_recorder = None

        # Per-PSI-target subdirectory
        test_dir = tests_dir / label
        test_dir.mkdir(exist_ok=True)
        
        try:
            # Start a new VM for each test, log files go to test_dir
            qemu, qmp, ssh, ps_proc = await setup_vm(args.qemu, args.kernel, args, swap_path, root, test_dir)

            await sleep(1)

            ts_part = _artifact_time_suffix(args)
            output_file_name = (
                f"rss_{label}_{ts_part}.csv" if ts_part else f"rss_{label}.csv"
            )
            output_file_path = test_dir / output_file_name

            rss_recorder = RSSRecorder(
                ps_proc,
                output_file_path,
                ssh,
                max_bytes,
                psi_target,
                debug=args.debug,
                cgroup_path=args.app_cgroup_path,
                reclaim_step_pages_grow=args.reclaim_step_pages_grow,
                reclaim_step_pages_shrink=args.reclaim_step_pages_shrink,
            )
            rss_recorder.start()
            print(
                f"blowfish-auto: host cgroup PSI at {args.app_cgroup_path} (QEMU PID {ps_proc.pid}), "
                "guest reclaim via /sys/kernel/reclaim/pagenums"
            )
            print(f"Started recording RSS with {print_label}")
            print(f"Started recording RSS and VM memory to {output_file_path}")

            print("Running application...")
            test_info = label
            results = await run_app_in_vm(ssh, args, result_dir=test_dir, test_info=test_info)

            # Stop recording and generate chart
            if rss_recorder and rss_recorder.running:
                rss_recorder.stop()

                chart_path = await asyncio.to_thread(plot_rss_chart, output_file_path, max_bytes)
                print(f"Generated RSS chart at {chart_path}")

                # Calculate statistics
                timestamps = []
                rss_values = []
                with open(output_file_path, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        timestamps.append(float(row['timestamp']))
                        rss_values.append(int(row['rss_bytes']))

                if rss_values and timestamps:
                    stats = compute_reclamation_stats(rss_values, timestamps, max_bytes)
                    reclamation_ratio = stats["reclamation_ratio"]

                    read_cell, relabel_cell, trial_cell = ("", "", "")
                    if results:
                        read_cell, relabel_cell, trial_cell = extract_tc_summary_metrics(
                            args, results[0]
                        )

                    # Append row to summary.csv
                    with open(summary_file, 'a') as f:
                        rel_path = os.path.relpath(chart_path, tests_dir) if chart_path else ""
                        f.write(
                            f"{target_val},{reclamation_ratio:.4f},"
                            f"{read_cell},{relabel_cell},{trial_cell},{rel_path}\n"
                        )

                    print(f"Memory statistics for {print_label}:")
                    print(f"  Reclamation ratio: {reclamation_ratio:.2%}")
                    if read_cell:
                        print(f"  Read Time (tc): {read_cell}")
                    if relabel_cell:
                        print(f"  Relabel (tc): {relabel_cell}")
                    if trial_cell:
                        print(f"  Trial Time (tc): {trial_cell}")
            
        except Exception as e:
            print(f"Error during {print_label} test: {e}")
            import traceback
            traceback.print_exc()
            
            with open(test_dir / "error.txt", 'w') as f:
                f.write(f"Error during test: {e}\n")
                traceback.print_exc(file=f)
                
        finally:
            print(f"Finishing {print_label} test...")
            if rss_recorder and rss_recorder.running:
                rss_recorder.stop()
            elif rss_recorder:
                rss_recorder.teardown_host_cgroup()

            if qmp:
                await qmp.disconnect()
            
            try:
                if ssh:
                    print("Reading VM dmesg logs...")
                    dmesg_output = await ssh.output("dmesg")
                    dmesg_path = test_dir / "dmesg.txt"
                    with open(dmesg_path, 'w') as dmesg_file:
                        dmesg_file.write(dmesg_output)
                    print(f"Saved VM dmesg logs to {dmesg_path}")
            except Exception as e:
                print(f"Warning: Failed to read VM dmesg: {e}")

            if qemu:
                initial_output = rm_ansi_escape(non_block_read(qemu.stdout))
                qemu.terminate()
                await sleep(3)
                final_output = rm_ansi_escape(non_block_read(qemu.stdout))
                try:
                    output_txt_path = test_dir / "output.txt"
                    with open(output_txt_path, 'w') as logfile:
                        logfile.write(initial_output)
                        logfile.write(final_output)
                except Exception as e:
                    print(f"Error saving QEMU output: {e}")
                
            # Wait for VM to fully terminate
            await sleep(5)

    print("\nAutomatic traversal completed!")
    print(f"Results saved in {tests_dir}")

if __name__ == "__main__":
    asyncio.run(main())