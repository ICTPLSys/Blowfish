# GAPBS TC

This directory holds [`test_tc.sh`](test_tc.sh), which the Blowfish bench expects on the VM. The workload is **Triangle counting (TC)** from the [GAP Benchmark Suite](https://github.com/sbeamer/gapbs).

## Install GAPBS inside the VM

Example (Debian/Ubuntu guest, user `debian`):

```bash
mkdir -p ~/code_tc
cd ~/code_tc
git clone https://github.com/sbeamer/gapbs.git
cd gapbs
make -j"$(nproc)"
cp tc converter ~/code_tc/
cp /usr/bin/time ~/code_tc/time
chmod +x ~/code_tc/tc ~/code_tc/converter ~/code_tc/time
```

Copy `test_tc.sh` from this repo to `~/code_tc/test_tc.sh` and run `chmod +x ~/code_tc/test_tc.sh`.

## Twitter graph (SNAP)

A common public source is the Stanford SNAP **Twitter-2010** graph:

- Dataset page: [https://snap.stanford.edu/data/twitter-2010.html](https://snap.stanford.edu/data/twitter-2010.html)  
  (dataset: *Social circles: Twitter*, `soc-twitter-2010.txt.gz`).

**Typical pipeline**

1. Download and decompress the SNAP file on a machine with enough disk and RAM.
2. Convert the dataset with the GAPBS `converter`.

## References

- GAPBS repository: [https://github.com/sbeamer/gapbs](https://github.com/sbeamer/gapbs) 
- SNAP Twitter-2010: [https://snap.stanford.edu/data/twitter-2010.html](https://snap.stanford.edu/data/twitter-2010.html)
