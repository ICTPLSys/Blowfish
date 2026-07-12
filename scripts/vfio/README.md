# VFIO and SR-IOV

Some systems expects a PCI device (for example an InfiniBand NIC) to be passed through to the VM. This directory provides helper scripts; adjust them for your NIC model, PCI addresses, and SR-IOV policy.

## Prerequisites

- **IOMMU** enabled in firmware and kernel (`intel_iommu=on`).
- **VFIO** modules available (`vfio_iommu_type1`).
- **SR-IOV** enabled on the physical NIC if you use VFs.

## Scripts

### `enable_vf.sh`

Example script that:

- Requests a number of **VFs** on a **Mellanox ConnectX-5** device (`mlx5_num_vfs`).
- Writes **node / port / policy** under `/sys/class/infiniband/mlx5_0/...`.
- **Unbinds and rebinds** specific PCI functions (`0000:b1:00.2`, `0000:b1:00.3`) to refresh VFs after SR-IOV changes.

Edit paths, PCI **BDF**s (`b1:00.x`), and InfiniBand device names (`mlx5_0`) for your machine. After SR-IOV is enabled successfully, you should see VFs, for example:

```bash
lspci | grep -i infiniband
# you should see output similar to
b1:00.2 Infiniband controller: Mellanox Technologies MT27800 Family [ConnectX-5 Virtual Function]
b1:00.3 Infiniband controller: Mellanox Technologies MT27800 Family [ConnectX-5 Virtual Function]
```

### `bind_vfio.py`

Interactive helper that lists **IOMMU groups** and binds the selected group’s PCI device(s) to `vfio-pci`.

```bash
sudo python3 bind_vfio.py
# you should see output similar to
259
  b1:00.2 Infiniband controller [0207]: Mellanox Technologies MT27800 Family [ConnectX-5 Virtual Function] [15b3:1018]
260
  b1:00.3 Infiniband controller [0207]: Mellanox Technologies MT27800 Family [ConnectX-5 Virtual Function] [15b3:1018]
Group id: 
# enter 260 for example, and remember b1:00.3
```

## Using the BDF with benchmarks

By default, QEMU is started with a `vfio-pci` device whose `host` is `0000:b1:00.3`. Override the VF BDF with `--vfio-dev`, or check the defaults in `run_all.py`:

```bash
./run_all.py --vfio-dev b1:00.3 ...
```
