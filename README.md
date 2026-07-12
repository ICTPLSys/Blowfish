# Blowfish: Elastic Virtual Machine Memory for Disaggregated Memory

Blowfish is a VM memory overcommitment framework built on disaggregated
memory, which performs timely cold (and free) memory reclamation
and restoration.

For more details, please refer to our work, [Blowfish](https://www.usenix.org/conference/osdi26/presentation/zhang-yulong), published at OSDI 2026.


## Repository layout

```
blowfish/
├── setup.sh                 # Host preparation (see Build and Install Blowfish)
├── bench/
│   ├── run_all.py           # Main evaluation orchestration script
│   ├── run_auto.py          # Single benchmark run
│   ├── build.py             # Build guest kernels and QEMU in one step
│   ├── scripts/             # QEMU, SSH, cgroup helpers, etc.
│   ├── vm/                  # VM helper scripts
│   ├── resources/           # Place your VM disk image here
│   └── results/             # Default results directory
├── scripts/
│   ├── network/             # Example libvirt default.xml + README
│   ├── vfio/                # Enable VFs and bind VFIO
│   ├── remoteswap/          # Swapping over RDMA
│   ├── vm/                  # Scripts to SSH into VM or destroy VM
│   └── cpu_freq/            # Pin frequency, disable turbo, etc.
├── guest-linux-6.1.0/       # Guest kernel
├── host-linux-6.1.0/        # Host kernel
├── qemu/                    # QEMU
├── env/                     # Conda environment file
├── apps/                    # Application scripts
└── ...
```

## Environment and Dependencies

- **Virtualization**: working `/dev/kvm`; Intel with **EPT** recommended.
- **RDMA / Remoteswap**: Blowfish builds on an RDMA-based remoteswap stack. See [`scripts/remoteswap/README.md`](scripts/remoteswap/README.md).
- **VFIO / PCI passthrough**: required for modes that pass through a device. See [`scripts/vfio/README.md`](scripts/vfio/README.md).
- **Networking**: scripts default to `--net-mode libvirt`; you need a libvirt network. See [`scripts/network/README.md`](scripts/network/README.md).
- **QEMU / guest image**: prepare a compatible QEMU disk image under `bench/resources` before the first run.
- **Privileges**: scripts may invoke `sudo` for cgroup setup or delegation.


## Build and Install Blowfish

### Host kernel

```bash
cd /path/to/blowfish/host-linux-6.1.0
./build_kernel.sh build
sudo ./build_kernel.sh install
# Update GRUB and reboot
```

After reboot, check **transparent huge pages** (THP):

```bash
cat /sys/kernel/mm/transparent_hugepage/enabled
# expect [always]
```

To apply `always` until the next reboot:

```bash
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

To make it **persistent**, add `transparent_hugepage=always` to the kernel command line (for example in `/etc/default/grub` under `GRUB_CMDLINE_LINUX_DEFAULT`), run `sudo update-grub`, then reboot.

### KVM / EPT

```bash
sudo cat /sys/module/kvm/parameters/tdp_mmu
# If the value is `N`,
# add to `/etc/modprobe.d/kvm.conf`, for example:
options kvm tdp_mmu=Y
# Reload modules and verify tdp_mmu again.
```


### Remoteswap

Install Mellanox OFED drivers on both machines (host node and memory node) with InfiniBand connected. On the memory node:

```bash
cd /path/to/blowfish/scripts/remoteswap/server
make
# ./rswap-server <memory-server-ip> <memory-server-port> <pool-size-GB> <cores-on-host-server>
```

On the cpu node:

```bash
cd /path/to/blowfish/scripts/remoteswap/client
make
# Edit manage_rswap_client.sh:
# set IP, port, pool size, swap file path, etc.
```

For details, see [`scripts/remoteswap/README.md`](scripts/remoteswap/README.md).

### SR-IOV / VFIO

The repo-root `setup.sh` invokes `enable_vf.sh` and `bind_vfio.py` before evaluation. The goal is to virtualize the InfiniBand NIC and pass it through to the VM.

Edit `scripts/vfio/enable_vf.sh` (or your own SR-IOV enablement scripts) for your topology. After SR-IOV is configured successfully, you should see devices similar to:

```bash
lspci | grep ib
b1:00.2 Infiniband controller: Mellanox Technologies MT27800 Family [ConnectX-5 Virtual Function]
b1:00.3 Infiniband controller: Mellanox Technologies MT27800 Family [ConnectX-5 Virtual Function]
```

For details, see [`scripts/vfio/README.md`](scripts/vfio/README.md).

### Networking

Configure networking so the guest has connectivity. We ship an example libvirt network definition. After configuring the network, verify:

```bash
virsh net-list --all
virsh net-dumpxml default
```

For details, see [`scripts/network/README.md`](scripts/network/README.md).

### One-shot host prep: `setup.sh`

```bash
cd /path/to/blowfish
bash setup.sh
```

`setup.sh` currently:

1. Runs `sudo /etc/init.d/openibd restart` and waits for the RDMA stack.
2. Runs `make clean && make` under `scripts/remoteswap/client/`, then `sudo ./manage_rswap_client.sh install` to install remoteswap (configure Remoteswap first).
3. Runs `sudo chown "$USER" /dev/kvm` so your user can create VMs.
4. CPU: disables turbo and applies fixed-frequency helpers (`scripts/cpu_freq/`).
5. SR-IOV: runs `scripts/vfio/enable_vf.sh` (configure VFIO / SR-IOV first).
6. VFIO bind: runs `sudo python3 scripts/vfio/bind_vfio.py`, which lists IOMMU groups. Enter the **group id** for the InfiniBand VF you want, then note its BDF (e.g. `b1:00.3`). Match that BDF to the defaults in the bench scripts or pass `--vfio-dev` to `run_all.py` / `run_auto.py` where required.

### Guest-side preparation

To boot a VM, provide a suitable disk image under the expected path (`bench/resources`); for example, you can create one with `virt-install` or get one from [https://www.debian.org/distrib/](https://www.debian.org/distrib/). The default guest login in the scripts is user `debian`, and passwordless `sudo` for that user is expected. If you change the username, update the relevant bench/VM scripts accordingly.

```bash
conda env create -f /path/to/blowfish/env/environment.yml
conda activate blowfish
cd /path/to/blowfish/bench
# Build guest kernels and QEMU (LLVM required)
./build.py
# Install applications and upload benchmark scripts, see apps/
```

## Applications

- `APP_SCRIPT_PRESETS` in [`bench/run_auto.py`](bench/run_auto.py) defines benchmark presets (e.g., `tc` -> `/home/debian/code_tc/test_tc.sh`).

- `APP_RESOURCE_PRESETS` in [`bench/run_all.py`](bench/run_all.py) supplies default **guest RAM (`-m`)** and **vCPU** (`-c`) when omitted.

**Adding a new preset**: extend `APP_SCRIPT_PRESETS` in `run_auto.py`, add matching `APP_RESOURCE_PRESETS` in `run_all.py`, place the script on the guest at the declared path, then run with `--app yourname`.

See [`apps/`](apps) for more details about different applications.

## Evaluation

```bash
cd /path/to/blowfish/bench
# Run the full evaluation suite
./run_all.py
# Or list options
./run_all.py --help
```

**Outputs**: by default under `/path/to/blowfish/bench/results/`.

`run_all.py` may invoke `sudo` several times. The script tries to refresh credentials early, but you can still be prompted again if the sudo timestamp expires. To **extend the password cache**, run `sudo visudo` and add a line such as:

```text
Defaults timestamp_timeout=120
```

## Related Repositories

+ [Hermit](https://github.com/uclasystem/hermit): Public remoteswap prototype.
+ [Hyperalloc](https://github.com/luhsra/hyperalloc-bench): Blowfish's baseline.
