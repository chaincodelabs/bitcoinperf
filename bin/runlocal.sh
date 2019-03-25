#!/usr/bin/env bash

export IBD_PEER_ADDRESS=localhost
export SYNCED_DATA_DIR=/data/bitcoin_bench
export SYNCED_BITCOIN_REPO_DIR="${HOME}/src/bitcoin_bench"
export CODESPEED_URL=http://localhost:8000
export CODESPEED_USER=admin
export CODESPEED_PASSWORD=password
export CODESPEED_ENVNAME=ccl-bench-hdd-1
export MAKE_JOBS="$(nproc --ignore=1)"
export COMPILERS=clang
export BENCHES_TO_RUN=ibd
export IBD_CHECKPOINTS=2_000,10_000,20_000,80_000
export NO_CLEAN=1
export NO_TEARDOWN=1

# Set minimumchainwork low so that we actually latch out of IBD at low
# heights.
export SYNCED_BITCOIND_ARGS=-minimumchainwork=0x00

if [ ! -f "/tmp/bitcoinperf-workdir" ]; then
  export BENCHES_TO_RUN=gitclone,build
  bitcoinperf --no-caution --commits "v0.16.0,master" | \
    grep "leaving workdir at" | \
    grep -Eo "(/tmp/.*)" \
    > /tmp/bitcoinperf-workdir
else
  bitcoinperf --no-caution --commits "master" --workdir "$(cat /tmp/bitcoinperf-workdir)" "$@"
fi
