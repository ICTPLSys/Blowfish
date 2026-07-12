#!/usr/bin/env python3
"""
Orchestrate ``run_auto.py`` modes: baseline ``all-local``, then PSI search
for ``blowfish-auto``.

Without ``--test``: stable session directory (default ``run_all``), no ``run_auto --time``,
and steps whose outputs already exist are skipped. With ``--test``: timestamped session
unless ``--session`` is set, and optional ``--time`` on ``run_auto`` as before.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

BENCH_DIR = Path(__file__).resolve().parent
RUN_AUTO = BENCH_DIR / "run_auto.py"

# Guest memory / vCPU presets (extend when adding --app presets)
APP_RESOURCE_PRESETS: dict[str, tuple[int, int]] = {
    "tc": (22, 4),
}

# Order after baseline
MODES_AFTER_BASELINE: tuple[str, ...] = (
    "blowfish-auto",
)

# Labels for result dirs, log files, and charts (internal --mode unchanged for run_auto.py).
MODE_OUTPUT_LABEL: dict[str, str] = {
    "blowfish-auto": "blowfish",
}


def mode_output_label(mode: str) -> str:
    return MODE_OUTPUT_LABEL.get(mode, mode)


def modes_requiring_vfio(mode: str) -> bool:
    return mode not in ("blowfish-auto",)


def parse_tc_relabel_trial(text: str) -> tuple[float | None, float | None, float | None]:
    """Parse Read Time / Relabel / Trial Time from tc-style stdout."""

    def grab(label: str) -> float | None:
        m = re.search(
            rf"^\s*{re.escape(label)}\s+([\d.]+)\s*$",
            text,
            re.MULTILINE,
        )
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    read_t = grab("Read Time:")
    rel = grab("Relabel:")
    tri = grab("Trial Time:")
    return (read_t, rel, tri)


def find_app_result_under(base: Path) -> Path | None:
    """Pick newest ``app_result*.txt`` under ``base`` (recursive)."""
    candidates = sorted(
        base.rglob("app_result*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def read_baseline_relabel_plus_trial(baseline_dir: Path) -> float:
    """Baseline = Relabel + Trial Time from all-local app output."""
    # run_auto: root / "all-local" / app_result_all-local.txt
    cand = baseline_dir / "all-local" / "app_result_all-local.txt"
    if cand.is_file():
        app_path = cand
    else:
        found = find_app_result_under(baseline_dir)
        if found is None:
            raise FileNotFoundError(
                f"No app_result*.txt under baseline tree {baseline_dir}"
            )
        app_path = found
    body = app_path.read_text(encoding="utf-8", errors="replace")
    _, relabel, trial = parse_tc_relabel_trial(body)
    if relabel is None or trial is None:
        raise ValueError(
            f"Could not parse Relabel/Trial Time from {app_path}"
        )
    return relabel + trial


def read_last_summary_row(summary_csv: Path) -> dict[str, str]:
    if not summary_csv.is_file():
        raise FileNotFoundError(f"Missing summary: {summary_csv}")
    with summary_csv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"Empty summary: {summary_csv}")
    return rows[-1]


def relabel_plus_trial_from_summary_row(row: dict[str, str]) -> float:
    rel = row.get("relabel", "").strip()
    tri = row.get("trial_time", "").strip()
    if not rel or not tri:
        raise ValueError(f"summary row missing relabel/trial_time: {row!r}")
    return float(rel) + float(tri)


def baseline_outputs_ready(baseline_dir: Path) -> bool:
    """True if existing baseline tree has parseable Relabel + Trial."""
    try:
        read_baseline_relabel_plus_trial(baseline_dir)
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


def mode_run_outputs_ready(run_dir: Path) -> bool:
    """True if ``summary.csv`` exists and last row has relabel + trial_time."""
    summary = run_dir / "summary.csv"
    if not summary.is_file():
        return False
    try:
        row = read_last_summary_row(summary)
        relabel_plus_trial_from_summary_row(row)
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


@dataclass
class RunRecord:
    mode: str
    suffix: str
    psi: float | None
    cmd: list[str]
    returncode: int = 0
    slowdown_pct: float | None = None
    relabel_plus_trial: float | None = None
    reclamation_ratio: float | None = None
    skipped: bool = False


@dataclass
class SessionState:
    session_root: Path
    records: list[RunRecord] = field(default_factory=list)
    port: int = 5300
    qmp: int = 5400

    def next_ports(self) -> tuple[int, int]:
        p, q = self.port, self.qmp
        self.port += 10
        self.qmp += 10
        return p, q


def build_run_auto_cmd(
    *,
    mode: str,
    mem: int,
    cores: int,
    app: str,
    session_parent: Path,
    suffix: str,
    port: int,
    qmp: int,
    vfio_dev: str | None,
    psi: Sequence[float] | None,
    time_flag: bool,
    net_mode: str,
    guest_ip: str | None,
    guest_gateway: str | None,
    guest_netmask: str | None,
    bridge_name: str | None,
    net_mac: str | None,
    libvirt_net: str,
    libvirt_domain: str,
    host_numa_node: int,
    extra_forward: list[str],
) -> list[str]:
    cmd: list[str] = [
        sys.executable,
        str(RUN_AUTO),
        "--mode",
        mode,
        "-m",
        str(mem),
        "-c",
        str(cores),
        "--app",
        app,
        "--root",
        str(session_parent),
        "--suffix",
        suffix,
        "--port",
        str(port),
        "--qmp",
        str(qmp),
        "--net-mode",
        net_mode,
        "--libvirt-net",
        libvirt_net,
        "--libvirt-domain",
        libvirt_domain,
        "--host-numa-node",
        str(host_numa_node),
    ]
    if time_flag:
        cmd.append("--time")
    if vfio_dev:
        cmd.extend(["--vfio-dev", vfio_dev])
    if psi is not None:
        cmd.append("--psi")
        cmd.extend(str(p) for p in psi)
    if net_mode == "libvirt":
        if guest_ip:
            cmd.extend(["--guest-ip", guest_ip])
        if guest_gateway:
            cmd.extend(["--guest-gateway", guest_gateway])
        if guest_netmask:
            cmd.extend(["--guest-netmask", guest_netmask])
        if bridge_name:
            cmd.extend(["--bridge-name", bridge_name])
        if net_mac:
            cmd.extend(["--net-mac", net_mac])
    cmd.extend(extra_forward)
    return cmd


def plot_slowdown_vs_reclamation(
    session_root: Path, manifest: dict
) -> Path | None:
    """
    One line per system (mode): X = reclamation ratio, Y = slowdown vs baseline (%).
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("Warning: matplotlib not installed; skipping slowdown_vs_reclamation chart.")
        return None

    runs = manifest.get("runs") or []
    series: dict[str, list[tuple[float, float]]] = {m: [] for m in MODES_AFTER_BASELINE}
    for r in runs:
        mode = r.get("mode")
        if mode not in series:
            continue
        if r.get("returncode") != 0:
            continue
        rr = r.get("reclamation_ratio")
        sd = r.get("slowdown_pct")
        if rr is None or sd is None:
            continue
        try:
            series[mode].append((float(rr), float(sd)))
        except (TypeError, ValueError):
            continue

    if not any(series[m] for m in MODES_AFTER_BASELINE):
        print("Warning: no (reclamation_ratio, slowdown) points to plot.")
        return None

    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728")
    for idx, mode in enumerate(MODES_AFTER_BASELINE):
        pts = sorted(series[mode], key=lambda t: t[0])
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(
            xs,
            ys,
            marker="o",
            linestyle="-",
            color=colors[idx % len(colors)],
            label=mode_output_label(mode),
        )

    ax.set_xlabel("Reclamation ratio")
    ax.set_ylabel("Slowdown vs baseline (%)")
    ax.set_title("Slowdown vs reclamation ratio")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = session_root / "slowdown_vs_reclamation.pdf"
    fig.savefig(out, format="pdf", dpi=150)
    plt.close(fig)
    print(f"Chart saved to {out}")
    return out


def run_subprocess(cmd: list[str], log_path: Path | None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(" ".join(shlex.quote(x) for x in cmd) + "\n\n")
        logf.flush()
        p = subprocess.run(
            cmd,
            cwd=str(BENCH_DIR),
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    return p.returncode


def ensure_sudo_credentials() -> None:
    """Run ``sudo -v`` up front so later ``run_auto.py`` / child ``sudo`` calls reuse the timestamp."""
    print("Refreshing sudo credentials (you may be prompted for a password)...", flush=True)
    try:
        subprocess.run(["sudo", "-v"], check=True)
    except FileNotFoundError:
        print("Warning: `sudo` not found in PATH; skipping credential check.", file=sys.stderr)
    except subprocess.CalledProcessError:
        print("error: `sudo -v` failed; aborting.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run all-local baseline, then blowfish-auto with adaptive PSI search vs "
            "baseline (relabel+trial). "
            "PSI search defaults: start 0.5, clamp to [--psi-min, --psi-max] (0..1), float steps."
        )
    )
    parser.add_argument(
        "--app",
        default="tc",
        help="Guest preset name passed to run_auto (default: tc)",
    )
    parser.add_argument(
        "-m",
        "--mem",
        type=int,
        default=None,
        help="Override guest RAM (GiB); default from app preset",
    )
    parser.add_argument(
        "-c",
        "--cores",
        type=int,
        default=None,
        help="Override vCPUs; default from app preset",
    )
    parser.add_argument(
        "--session",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Directory name under --results-parent. "
            "Default without --test: run_all (stable; enables resume). "
            "Default with --test: run_all-<yyMMdd-HHMMSS>."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Always run benchmarks and honor --time on run_auto (timestamp-prefixed dirs). "
            "Without --test, omit run_auto --time, default session is stable, and steps with "
            "existing valid outputs are skipped."
        ),
    )
    parser.add_argument(
        "--results-parent",
        type=Path,
        default=Path("results"),
        help="Parent directory for this session (default: bench/results)",
    )
    parser.add_argument(
        "--time",
        action="store_true",
        help="Forward --time to each run_auto (yyMMdd-HHMMSS- prefix on result dirs). "
        "Only applied together with --test (ignored without --test).",
    )
    parser.add_argument(
        "--vfio-dev",
        type=str,
        default="b1:00.3",
        help="BDF for vfio (default b1:00.3); omitted for blowfish-auto",
    )
    parser.add_argument(
        "--initial-psi",
        type=float,
        default=0.5,
        help="Starting PSI for search (default: 0.5)",
    )
    parser.add_argument(
        "--psi-min",
        type=float,
        default=0.0,
        help="Lower bound when adjusting PSI (default: 0)",
    )
    parser.add_argument(
        "--psi-max",
        type=float,
        default=1.0,
        help="Upper bound when adjusting PSI (default: 1)",
    )
    parser.add_argument(
        "--psi-changes",
        type=int,
        default=5,
        help="Number of PSI adjustments (bench runs) per mode after baseline (default: 5)",
    )
    parser.add_argument(
        "--slowdown-threshold-pct",
        type=float,
        default=5.0,
        help="If slowdown vs baseline is below this %%, increase PSI; above, decrease (default: 5)",
    )
    parser.add_argument(
        "--initial-psi-step",
        type=float,
        default=0.1,
        help="Initial PSI step (float); halves on direction reversal (default: 0.1)",
    )
    parser.add_argument(
        "--min-psi-step",
        type=float,
        default=0.01,
        help="Minimum PSI step after adaptation; must be > 0 (default: 0.01)",
    )
    parser.add_argument(
        "--net-mode",
        choices=["user", "libvirt"],
        default="libvirt",
    )
    parser.add_argument("--guest-ip", default=None)
    parser.add_argument("--guest-gateway", default=None)
    parser.add_argument("--guest-netmask", default=None)
    parser.add_argument("--bridge-name", default=None)
    parser.add_argument("--net-mac", default=None)
    parser.add_argument("--libvirt-net", default="default")
    parser.add_argument("--libvirt-domain", default="test")
    parser.add_argument("--host-numa-node", type=int, default=0)
    parser.add_argument(
        "run_auto_forward",
        nargs="*",
        metavar="EXTRA",
        help="Extra args appended to each run_auto invocation (e.g. --debug)",
    )
    args = parser.parse_args()

    if args.min_psi_step <= 0:
        parser.error("--min-psi-step must be > 0")
    if args.initial_psi_step <= 0:
        parser.error("--initial-psi-step must be > 0")
    if args.psi_min >= args.psi_max:
        parser.error("--psi-min must be less than --psi-max")
    if not (args.psi_min <= args.initial_psi <= args.psi_max):
        parser.error(
            f"--initial-psi must be within [--psi-min, --psi-max], "
            f"got {args.initial_psi} not in [{args.psi_min}, {args.psi_max}]"
        )

    ensure_sudo_credentials()

    if args.app not in APP_RESOURCE_PRESETS:
        if args.mem is None or args.cores is None:
            parser.error(
                f"Unknown app {args.app!r}: add to APP_RESOURCE_PRESETS or pass -m and -c"
            )
    preset_mem, preset_cores = APP_RESOURCE_PRESETS.get(args.app, (None, None))
    mem = args.mem if args.mem is not None else preset_mem
    cores = args.cores if args.cores is not None else preset_cores
    assert mem is not None and cores is not None

    if args.session is not None:
        session_name = args.session
    elif args.test:
        session_name = f"run_all-{datetime.now().strftime('%y%m%d-%H%M%S')}"
    else:
        session_name = "run_all"

    use_run_auto_time = bool(args.test and args.time)

    session_parent: Path = (BENCH_DIR / args.results_parent).resolve()
    session_root = session_parent / session_name
    session_root.mkdir(parents=True, exist_ok=True)

    state = SessionState(session_root=session_root)

    manifest: dict = {
        "session": session_name,
        "test_mode": args.test,
        "run_auto_time": use_run_auto_time,
        "app": args.app,
        "mem": mem,
        "cores": cores,
        "modes": ["all-local", *MODES_AFTER_BASELINE],
        "psi_changes_per_mode": args.psi_changes,
        "slowdown_threshold_pct": args.slowdown_threshold_pct,
        "psi_min": args.psi_min,
        "psi_max": args.psi_max,
        "runs": [],
    }

    def log_run(rec: RunRecord) -> None:
        state.records.append(rec)
        entry: dict = {
            "mode": rec.mode,
            "output_label": mode_output_label(rec.mode),
            "suffix": rec.suffix,
            "psi": rec.psi,
            "returncode": rec.returncode,
            "slowdown_pct": rec.slowdown_pct,
            "relabel_plus_trial": rec.relabel_plus_trial,
            "reclamation_ratio": rec.reclamation_ratio,
        }
        if rec.skipped:
            entry["skipped"] = True
        manifest["runs"].append(entry)

    # ---------- Baseline: all-local ----------
    base_suffix = "baseline-all-local"
    port, qmp = state.next_ports()
    vfio = args.vfio_dev if modes_requiring_vfio("all-local") else None
    base_cmd = build_run_auto_cmd(
        mode="all-local",
        mem=mem,
        cores=cores,
        app=args.app,
        session_parent=session_parent,
        suffix=f"{session_name}/{base_suffix}",
        port=port,
        qmp=qmp,
        vfio_dev=vfio,
        psi=None,
        time_flag=use_run_auto_time,
        net_mode=args.net_mode,
        guest_ip=args.guest_ip,
        guest_gateway=args.guest_gateway,
        guest_netmask=args.guest_netmask,
        bridge_name=args.bridge_name,
        net_mac=args.net_mac,
        libvirt_net=args.libvirt_net,
        libvirt_domain=args.libvirt_domain,
        host_numa_node=args.host_numa_node,
        extra_forward=list(args.run_auto_forward),
    )
    # run_auto result root = --root / [--time prefix +] --suffix → …/session_name/baseline-all-local

    rec = RunRecord(mode="all-local", suffix=f"{session_name}/{base_suffix}", psi=None, cmd=base_cmd)
    log_path = session_root / "logs" / "00-baseline-all-local.log"
    baseline_tree = session_parent / session_name / base_suffix

    resume_ok = not args.test and baseline_outputs_ready(baseline_tree)
    if resume_ok:
        print("=== Baseline: all-local (skipped, existing outputs) ===")
        rec.skipped = True
        rec.returncode = 0
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "Skipped: valid baseline outputs already present under:\n"
            f"  {baseline_tree}\n",
            encoding="utf-8",
        )
        log_run(rec)
    else:
        print("=== Baseline: all-local ===")
        print(" ".join(shlex.quote(x) for x in base_cmd))
        rec.returncode = run_subprocess(base_cmd, log_path)
        log_run(rec)
        if rec.returncode != 0:
            print(f"ERROR: baseline failed (exit {rec.returncode}), see {log_path}", file=sys.stderr)
            (session_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            sys.exit(rec.returncode)

    try:
        baseline_sum = read_baseline_relabel_plus_trial(baseline_tree)
    except Exception as e:
        print(f"ERROR: could not read baseline: {e}", file=sys.stderr)
        manifest["baseline_error"] = str(e)
        (session_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        sys.exit(1)

    manifest["baseline_relabel_plus_trial"] = baseline_sum
    print(f"Baseline (Relabel + Trial Time): {baseline_sum:.6f}")

    # ---------- Each mode: adaptive PSI ----------
    for mode in MODES_AFTER_BASELINE:
        out_name = mode_output_label(mode)
        print(f"\n=== Mode: {out_name} ({mode}) - PSI search, {args.psi_changes} steps ===")
        psi = max(args.psi_min, min(args.psi_max, float(args.initial_psi)))
        step = float(args.initial_psi_step)
        prev_dir: int | None = None

        for i in range(args.psi_changes):
            suf = f"{session_name}/{out_name}-psi{i + 1:02d}-{psi:.2f}".replace(".", "p")
            port, qmp = state.next_ports()
            vfio = args.vfio_dev if modes_requiring_vfio(mode) else None
            cmd = build_run_auto_cmd(
                mode=mode,
                mem=mem,
                cores=cores,
                app=args.app,
                session_parent=session_parent,
                suffix=suf,
                port=port,
                qmp=qmp,
                vfio_dev=vfio,
                psi=[psi],
                time_flag=use_run_auto_time,
                net_mode=args.net_mode,
                guest_ip=args.guest_ip,
                guest_gateway=args.guest_gateway,
                guest_netmask=args.guest_netmask,
                bridge_name=args.bridge_name,
                net_mac=args.net_mac,
                libvirt_net=args.libvirt_net,
                libvirt_domain=args.libvirt_domain,
                host_numa_node=args.host_numa_node,
                extra_forward=list(args.run_auto_forward),
            )
            rec = RunRecord(mode=mode, suffix=suf, psi=psi, cmd=cmd)
            log_path = session_root / "logs" / f"{out_name}-iter{i + 1:02d}.log"
            run_dir = session_parent / suf
            slowdown_pct: float | None = None
            rpt: float | None = None

            filled_from_disk = False
            if not args.test and mode_run_outputs_ready(run_dir):
                try:
                    summary_path = run_dir / "summary.csv"
                    row = read_last_summary_row(summary_path)
                    rpt = relabel_plus_trial_from_summary_row(row)
                    slowdown_pct = (rpt - baseline_sum) / baseline_sum * 100.0 if baseline_sum > 0 else 0.0
                    rec.relabel_plus_trial = rpt
                    rec.slowdown_pct = slowdown_pct
                    rr_str = row.get("reclamation_ratio", "").strip()
                    rec.reclamation_ratio = float(rr_str) if rr_str else None
                    rec.skipped = True
                    rec.returncode = 0
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(
                        "Skipped: valid summary.csv already present under:\n"
                        f"  {run_dir}\n",
                        encoding="utf-8",
                    )
                    print(f"  iter {i + 1}/{args.psi_changes}: PSI={psi:.4f} (skipped, existing outputs)")
                    print(
                        f"    relabel+trial={rpt:.6f}  slowdown={slowdown_pct:+.2f}% "
                        f"(threshold {args.slowdown_threshold_pct:g}%) [from disk]"
                    )
                    filled_from_disk = True
                except Exception as ex:
                    print(f"    WARN: outputs present but unusable ({ex}); re-running.")

            if not filled_from_disk:
                rec.skipped = False
                print(f"  iter {i + 1}/{args.psi_changes}: PSI={psi:.4f}")
                print(" ", " ".join(shlex.quote(x) for x in cmd))
                rec.returncode = run_subprocess(cmd, log_path)
                if rec.returncode == 0:
                    try:
                        summary_path = run_dir / "summary.csv"
                        row = read_last_summary_row(summary_path)
                        rpt = relabel_plus_trial_from_summary_row(row)
                        slowdown_pct = (rpt - baseline_sum) / baseline_sum * 100.0 if baseline_sum > 0 else 0.0
                        rec.relabel_plus_trial = rpt
                        rec.slowdown_pct = slowdown_pct
                        rr_str = row.get("reclamation_ratio", "").strip()
                        rec.reclamation_ratio = float(rr_str) if rr_str else None
                        print(
                            f"    relabel+trial={rpt:.6f}  slowdown={slowdown_pct:+.2f}% "
                            f"(threshold {args.slowdown_threshold_pct:g}%)"
                        )
                    except Exception as ex:
                        print(f"    WARN: could not parse summary: {ex}")
                else:
                    print(f"    ERROR: run_auto exit {rec.returncode}, see {log_path}")

            log_run(rec)

            if rec.returncode != 0 or slowdown_pct is None:
                break

            direction: int
            if slowdown_pct < args.slowdown_threshold_pct:
                direction = 1
                psi = min(args.psi_max, psi + step)
            elif slowdown_pct > args.slowdown_threshold_pct:
                direction = -1
                psi = max(args.psi_min, psi - step)
            else:
                print("    slowdown within threshold band; stopping early.")
                break

            if prev_dir is not None and prev_dir != direction:
                step = max(args.min_psi_step, step * 0.5)
                print(f"    adapt: direction flip -> step={step:.6g}")
            prev_dir = direction

    out_manifest = session_root / "manifest.json"
    out_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSession manifest: {out_manifest}")

    plot_slowdown_vs_reclamation(session_root, manifest)


if __name__ == "__main__":
    main()
