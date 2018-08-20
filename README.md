## Bitcoin Core performance monitor 📈

This repository consists of two components

- a [codespeed](https://github.com/chaincodelabs/codespeed) installation which
  collects and presents benchmarking results in a web interface, and
- a haphazard Python script for running high-level Bitcoin Core benchmarks which
  POSTs results to codespeed.

The benchmarks which are monitored are

- Build time (make)
- Unittest duration (make check)
- Functional test framework duration (test/functional/test_runner.py)
- Microbenchmarks (bench-bitcoin)
- IBD up to some height from a local peer or from the P2P network
- Reindex up to some height

The Python script (`bitcoinperf`) may be used as a standalone script
(in conjunction with the Docker configuration) to benchmark and compare
different Bitcoin commits locally - without necessarily writing to a remote
codespeed instance.


### Example local usage

First, you may have to modify the `synced` mountpoint in `docker-compose.yml`
from `/data/bitcoin_bench` to a path on your machine that corresponds to a
Bitcoin datadir which is synced up to your desired stopatheight.

Install docker & docker-compose, then run

#
```sh
# Bring up codespeed server and a synced bitcoind instance

$ docker-compose up -d codespeed synced

# Modify docker-compose.yml to reference a synced datadir on your host machine.

$ sed -ie 's#/data/bitcoin_bench#/path/to/your/datadir#g' docker-compose.yml

# Compare v0.16.0 to the current tip

$ docker-compose run --rm bench \
    bitcoinperf \
    --commits "v0.16.0,master"
    --run-counts ibd:3 --benches-to-run gitclone,build,ibd --bitcoind-stopatheight 200000

```
