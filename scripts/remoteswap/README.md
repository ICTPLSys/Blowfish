# Remoteswap

This directory contains the **memory-server** and **cpu-client** pieces of an RDMA-based remote swap stack used for experiments with disaggregated memory. The code traces its lineage to publicly released [**Hermit**](https://github.com/uclasystem/hermit)-style remoteswap prototypes, with local changes (THP support) for integration and performance.

## Prerequisites

**Hardware:**

- InfiniBand: Mellanox ConnectX-5 tested.

**Software:**

- Ubuntu 20.04 has been tested.
- MLNX_OFED_LINUX-23.04-0.5.3.3 has been tested.

## Build

### OFED driver

Download the [**MLNX_OFED**](https://network.nvidia.com/products/infiniband-drivers/linux/mlnx_ofed/) bundle for your OS, unpack, and run the vendor installer (often `sudo ./mlnxofedinstall --add-kernel-support`). Restart `openibd` (or equivalent) and reboot if the installer requires it.

### Client (CPU node)

```bash
cd scripts/remoteswap/client
make
```

A successful build produces `rswap-client.ko` in this directory.

### Server (memory node)

```bash
cd scripts/remoteswap/server
make
```

This produces the `rswap-server` binary.

## Usage

### 1. Start the server (memory node)

The server must stay running (e.g. **`tmux`**).

You need the **memory server** listen address/port, pool size (GB), and the logical CPU count of the client host.

```bash
cd scripts/remoteswap/server
./rswap-server <memory-server-ip> <memory-server-port> <pool-size-GB> <host-logical-cpus>
# Example: ./rswap-server 172.16.32.1 9401 64 96
```

### 2. Configure the client (`manage_rswap_client.sh`)

On the host, edit `scripts/remoteswap/client/manage_rswap_client.sh`. Typical fields:

```bash
SWAP_PARTITION_SIZE_GB="64"
mem_server_ip="172.16.32.1"
mem_server_port="9401"
```

`SWAP_PARTITION_SIZE_GB` must match the pool size passed to `rswap-server`. `mem_server_ip` and `mem_server_port` must match the running server's listen address and port (and be reachable from this client).

### 3. Install the client module

```bash
cd scripts/remoteswap/client
sudo ./manage_rswap_client.sh install
```

Allocation and RDMA setup can take time. Check **`dmesg`** for success messages (e.g. chunks received from the memory server, RDMA session established, frontswap loaded).

## Blowfish integration

The `setup.sh` may build the client and run `manage_rswap_client.sh install` after OFED and script configuration are done. Configure `manage_rswap_client.sh` and memory node before relying on automation.
