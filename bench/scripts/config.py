from argparse import Action, ArgumentParser, Namespace
from collections.abc import Callable, Sequence
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent

DEFAULT_DISK = ROOT / "resources/debian.qcow2"
DEFAULT_SWAP = ROOT / "resources/swap.img"
DEFAULT_SWAP_SIZE_GB = 50

DEFAULTS = {
    "all-local": {
        "qemu": ROOT.parent / "qemu/build-virt/qemu-system-x86_64",
        "kernel": ROOT.parent / "guest-linux-6.1.0/build-all-local/arch/x86/boot/bzImage",
    },
    "blowfish": {
        "qemu": ROOT.parent / "qemu/build-virt/qemu-system-x86_64",
        "kernel": ROOT.parent / "guest-linux-6.1.0/build-blowfish/arch/x86/boot/bzImage",
    },
    "blowfish-auto": {
        "qemu": ROOT.parent / "qemu/build-virt/qemu-system-x86_64",
        "kernel": ROOT.parent / "guest-linux-6.1.0/build-blowfish/arch/x86/boot/bzImage",
    },
}


class ModeAction(Action):
    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        nargs: int | str | None = None,
        **kwargs,
    ) -> None:
        assert nargs is None, "nargs not allowed"
        super().__init__(option_strings, dest, nargs, **kwargs)

    def __call__(
        self,
        parser: ArgumentParser,
        namespace: Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        assert isinstance(values, str)
        assert (
            values in BALLOON_CFG.keys()
        ), f"mode has to be on of {list(BALLOON_CFG.keys())}"

        kind = values.split("-")[0] if values != "all-local" and values != "blowfish" and values != "blowfish-auto" else values
        assert kind in DEFAULTS, f"Unknown mode: {kind}"

        if namespace.qemu is None:
            namespace.qemu = str(DEFAULTS[kind]["qemu"])
        if namespace.kernel is None:
            namespace.kernel = str(DEFAULTS[kind]["kernel"])
        setattr(namespace, self.dest, values)


BALLOON_CFG: dict[str, Callable[[int, int, int, int], list[str]]] = {
    "all-local": lambda cores, mem, _min_mem, _init_mem: qemu_virtio_balloon_args(
        cores, mem, False
    ),
    "blowfish": lambda cores, mem, _min_mem, _init_mem: qemu_virtio_balloon_args(
        cores, mem, False
    ),
    "blowfish-auto": lambda cores, mem, _min_mem, _init_mem: qemu_virtio_balloon_args(
        cores, mem, True
    ),
}


def qemu_virtio_balloon_args(cores: int, mem: int, auto: bool) -> list[str]:
    return [
        "-m",
        f"{mem}G",
        "-device",
        json.dumps({"driver": "virtio-balloon", "free-page-reporting": auto}),
    ]

