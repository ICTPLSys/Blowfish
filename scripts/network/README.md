# Libvirt networking

This directory ships `default.xml`, an example libvirt network definition you can import as-is. Key fields in the sample:

- Network name: `default`
- Bridge: `virbr0`
- Gateway: `192.168.122.1`
- Netmask: `255.255.255.0`
- DHCP range: `192.168.122.2`–`192.168.122.254`
- Static DHCP host (example):
  - MAC `52:54:00:1a:fe:d1`
  - IP `192.168.122.10`

Adjust MAC/IP to match your VM if you use fixed DHCP reservations.

## What scripts do in libvirt mode

Under `bench/`, `run_auto.py` defaults to `--net-mode libvirt`. It then:

1. Runs `virsh net-dumpxml <network>` and parses the XML.
2. Reads **bridge name**, **IPv4 gateway**, and **netmask** from the `<ip>` / `<bridge>` elements.
3. Requires at least one **static DHCP reservation** in that XML: `<dhcp><host mac='…' ip='…'/>`. It uses the **first** such `<host>` as the default guest MAC and SSH IP when `--net-mac` / `--guest-ip` are omitted.
4. If you pass `--net-mac` and/or `--guest-ip`, those override the defaults from step 3.

`run_all.py` forwards the same networking flags to every child `run_auto.py` invocation.


## Install the network from `default.xml`

From this directory:

```bash
virsh net-define default.xml
virsh net-autostart default
virsh net-start default
```


## Verify

List networks:

```bash
virsh net-list --all
```

Expect something like:

```text
Name      State    Autostart   Persistent
--------------------------------------------
default   active   yes         yes
```

Inspect the definition:

```bash
virsh net-dumpxml default
```