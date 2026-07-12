#!/usr/bin/env python3

from argparse import ArgumentParser
import asyncio
from pathlib import Path
import shutil
import os


async def build():
    parent = Path(__file__).parent.parent

    async def run(cmd: str, cwd: Path):
        print(f"\n\x1b[94mRunning: {cmd}\n - CWD={cwd}\x1b[0m")
        cwd.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_shell(cmd, cwd=cwd)
        ret = await process.wait()
        assert ret == 0, f"Failed with {ret}"

    llvm_suffix = os.environ.get("HYPERALLOC_LLVM_SUFFIX")
    if not llvm_suffix:
        for suffix in ("16", "18", "17", "15", "14", "13", "12", "11", "10"):
            if shutil.which(f"clang-{suffix}"):
                llvm_suffix = suffix
                break
    if not llvm_suffix:
        raise RuntimeError(
            "No clang toolchain found. Install e.g. `sudo apt install clang-18 lld-18` "
            "or set HYPERALLOC_LLVM_SUFFIX to an installed version."
        )
    llvm_flag = f"LLVM=-{llvm_suffix}"
    print(f"\x1b[94mUsing {llvm_flag} (detected clang-{llvm_suffix})\x1b[0m")

    await run(f"make {llvm_flag} oldconfig O=build-all-local", cwd=parent / "guest-linux-6.1.0")
    await run(f"make {llvm_flag} -j`nproc` O=build-all-local", cwd=parent / "guest-linux-6.1.0")
    await run(f"make {llvm_flag} oldconfig O=build-blowfish", cwd=parent / "guest-linux-6.1.0")
    await run(f"make {llvm_flag} -j`nproc` O=build-blowfish", cwd=parent / "guest-linux-6.1.0")

    await run(
        f"CC=clang-{llvm_suffix} ../configure --enable-debug --target-list=x86_64-softmmu --enable-slirp && ninja",
        cwd=parent / "qemu/build-virt",
    )


async def main():
    await build()

if __name__ == "__main__":
    asyncio.run(main())
