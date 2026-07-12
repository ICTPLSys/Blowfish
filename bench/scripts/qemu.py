from asyncio import sleep
import json
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
import psutil
import sys

sys.path.append(str(Path(__file__).parent.parent))
from scripts.utils import non_block_read, rm_ansi_escape


def qemu_vm(
    qemu: str | Path = "qemu-system-x86_64",
    port: int = 5022,
    kernel: str | Path = "bzImage",
    cores: int = 8,
    sockets: int = 1,
    hda: str | Path = "resources/hda.qcow2",
    kvm: bool = True,
    qmp_port: int = 5023,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    vfio_device: str | None = None,
    slice: str | None = None,
    core_start: int = 0,
    network_mode: str = "libvirt",
    hostfwd_rules: list[str] | None = None,
    net_mac: str | None = None,
    bridge_name: str = "virbr0",
    host_numa_node: int = 0,
    pin: bool = True,
) -> Popen[str]:
    """Start a vm with the given configuration."""
    assert cores > 0 and cores % sockets == 0

    logical = psutil.cpu_count(logical=True)
    physical = psutil.cpu_count(logical=False)
    assert logical is not None and physical is not None
    assert cores <= logical
    assert Path(hda).exists()

    assert sockets == 1, "not supported"

    if not extra_args:
        extra_args = []
    extra_args = apply_host_numa_binding(extra_args, host_numa_node, cores)

    net_args = qemu_net_args(
        network_mode=network_mode,
        port=port,
        hostfwd_rules=hostfwd_rules,
        net_mac=net_mac,
        bridge_name=bridge_name,
    )

    kernel_cmdline = "root=/dev/sda3 console=ttyS0 nokaslr net.ifnames=0 biosdevname=0"

    base_args = [
        # fmt: off
        str(qemu),
        *qemu_bios_args(qemu),
        #"-m", f"{mem}G",
        "-smp", f"{cores}",
        "-hda", str(hda),
        # "-snapshot",
        "-serial", "mon:stdio",
        "-nographic",
        "-kernel", str(kernel),
        "-append", kernel_cmdline,
        "-qmp", f"tcp:localhost:{qmp_port},server=on,wait=off",
        *net_args,
        "-no-reboot",
        "--cpu", "host",
        *extra_args,
        *vfio_dev_arg(vfio_device),
    ]

    if slice:
        base_args = ["systemd-run", "--user", "--slice", slice, "--scope", *base_args]

    # Combine `-append`
    args = []
    cmdline = []
    is_append = False
    for arg in base_args:
        if is_append:
            cmdline.append(arg)
        elif arg != "-append":
            args.append(arg)
        is_append = arg == "-append"
    args += ["-append", " ".join(cmdline)]

    if kvm:
        args.append("-enable-kvm")

    process = Popen(args, stdout=PIPE, stderr=STDOUT, text=True, env=env)

    # Pin qemu to a cpuset on one numa node with one core per vcpu
    if pin:
        step = 1
        if logical > physical:
            print("\033[33mWARNING: SMT detected, results might be less stable!\033[0m")
            if (core_start + cores) <= physical:
                step = 1
                print("  \033[33mPinning on physical cores.\033[0m")
            else:
                print("  \033[33mPinning on logical cores.\033[0m")
        assert (core_start + cores * step) <= logical, "Not enough cores"

        cpu_set = [x * step for x in range(core_start, core_start + cores)]
        print(f"Pinning qemu to cores: {cpu_set} (start: {core_start}, step: {step})")

        q = psutil.Process(process.pid)
        q.cpu_affinity(cpu_set)

    return process


def qemu_bios_args(qemu_bin: str | Path) -> list[str]:
    """Add BIOS search path for local in-tree QEMU builds."""
    qemu_path = Path(qemu_bin).expanduser().resolve()
    candidates = [
        qemu_path.parent.parent / "pc-bios",  # e.g. qemu/build-virt/qemu-system-x86_64
        qemu_path.parent / "pc-bios",
        Path("/usr/share/qemu"),
        Path("/usr/share/seabios"),
    ]
    for bios_dir in candidates:
        if (bios_dir / "bios-256k.bin").exists():
            return ["-L", str(bios_dir)]
    return []


def apply_host_numa_binding(extra_args: list[str], host_numa_node: int, cores: int) -> list[str]:
    """Bind the guest's base RAM to a specific host NUMA node."""
    args = list(extra_args)
    for i, arg in enumerate(args):
        if arg != "-m" or i + 1 >= len(args):
            continue

        mem_size = args[i + 1]
        if "," in mem_size:
            raise ValueError(
                "Host NUMA binding currently requires a simple '-m <size>' memory config. "
                f"Got '-m {mem_size}'. Ensure CPU pinning and host NUMA placement stay aligned."
            )

        numa_args = [
            "-object",
            f"memory-backend-ram,id=ram0,size={mem_size},host-nodes={host_numa_node},policy=bind",
        ]
        return [*args, *numa_args]

    raise ValueError(
        "Host NUMA binding requires a '-m <size>' QEMU memory argument. "
        "Ensure CPU pinning and host NUMA placement stay aligned."
    )


def qemu_net_args(
    network_mode: str,
    port: int,
    hostfwd_rules: list[str] | None,
    net_mac: str | None,
    bridge_name: str,
) -> list[str]:
    """Build QEMU network arguments.

    user mode is compatible with localhost port forwarding.
    libvirt mode uses qemu-bridge-helper and the libvirt bridge.
    """
    if network_mode == "user":
        rules = hostfwd_rules or [
            f"tcp:127.0.0.1:{port}-:22",
            "tcp:127.0.0.1:11211-:11211",
        ]
        nic = "user"
        for rule in rules:
            nic += f",hostfwd={rule}"
        return ["-nic", nic]

    if network_mode == "libvirt":
        nic = f"bridge,br={bridge_name},model=virtio-net-pci"
        if net_mac:
            nic += f",mac={net_mac}"
        if helper := resolve_bridge_helper():
            nic += f",helper={helper}"
        return ["-nic", nic]

    raise ValueError(f"Unsupported network_mode: {network_mode}")


def resolve_bridge_helper() -> str | None:
    """Return a usable qemu-bridge-helper path when available."""
    for candidate in (
        Path("/usr/libexec/qemu-bridge-helper"),
        Path("/usr/lib/qemu/qemu-bridge-helper"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def vfio_dev_arg(dev: str | None) -> list[str]:
    if not dev:
        return []
    if len(dev) < 12:
        dev = f"0000:{dev}"
    path = Path("/sys/bus/pci/devices") / dev
    assert path.exists(), f"Device {dev} not found!"
    assert (path / "driver_override").read_text().strip() == "vfio-pci", f"Device {dev} not bound to vfio!"
    return ["-device", json.dumps({"driver": "vfio-pci", "host": dev})]


async def qemu_wait_startup(qemu: Popen[str], logfile: Path):
    count = 0
    while True:
        await sleep(3)
        assert qemu.poll() is None
        text = non_block_read(s) if (s := qemu.stdout) else ""
        if len(text) == 0:
            # no changes in the past seconds
            # we either finished or paniced
            if count > 2:
                break
            count += 1
        else:
            count = 0
        with logfile.open("a+") as f:
            f.write(rm_ansi_escape(text))

    assert qemu.poll() is None, "Qemu exited unexpectedly"
