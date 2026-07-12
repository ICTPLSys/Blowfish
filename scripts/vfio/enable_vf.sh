#!/bin/bash

set -e

# how many VFs, PF
echo 2 > /sys/class/infiniband/mlx5_0/device/mlx5_num_vfs
# for each node, port and policy
echo 11:22:33:44:77:20:01:90 > /sys/class/infiniband/mlx5_0/device/sriov/0/node
echo 11:22:33:44:77:20:01:91 > /sys/class/infiniband/mlx5_0/device/sriov/0/port
echo Follow > /sys/class/infiniband/mlx5_0/device/sriov/0/policy
echo 11:22:33:44:77:20:01:92 > /sys/class/infiniband/mlx5_0/device/sriov/1/node
echo 11:22:33:44:77:20:01:93 > /sys/class/infiniband/mlx5_0/device/sriov/1/port
echo Follow > /sys/class/infiniband/mlx5_0/device/sriov/1/policy
# reset (lspci)
echo 0000:b1:00.2 > /sys/bus/pci/drivers/mlx5_core/unbind
echo 0000:b1:00.2 > /sys/bus/pci/drivers/mlx5_core/bind
echo 0000:b1:00.3 > /sys/bus/pci/drivers/mlx5_core/unbind
echo 0000:b1:00.3 > /sys/bus/pci/drivers/mlx5_core/bind
