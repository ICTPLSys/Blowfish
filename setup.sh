#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo /etc/init.d/openibd restart
sleep 10
cd "$ROOT_DIR/scripts/remoteswap/client/"
make clean
make
sudo ./manage_rswap_client.sh install
cd "$ROOT_DIR"
sudo chown "$USER" /dev/kvm
sudo "$ROOT_DIR/scripts/cpu_freq/turbo-boost.sh" disable
sudo "$ROOT_DIR/scripts/cpu_freq/cpu-freq.sh" enable
# sr-iov
sudo "$ROOT_DIR/scripts/vfio/enable_vf.sh"
sleep 1
# vfio
sudo python3 "$ROOT_DIR/scripts/vfio/bind_vfio.py"
