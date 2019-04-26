#!/usr/bin/env bash

# This script is mostly used as a functional test. It's not really a useful
# configuration for benchmarking.
#
# The first run initializes a built working directory containing a compiled
# bitcoind. Subsequent runs perform an ibd without needing to do time-consuming
# rebuilds.

set -euo pipefail

export IBD_PEER_ADDRESS=localhost
export SYNCED_DATADIR=/data/bitcoin_bench
export SYNCED_BITCOIN_REPO_DIR="${HOME}/src/bitcoin_bench"
export CODESPEED_URL=http://localhost:8000
export CODESPEED_USER=admin
export CODESPEED_PASSWORD=password
export CODESPEED_ENVNAME=ccl-bench-hdd-1
export MAKE_JOBS="$(nproc --ignore=1)"
export COMPILERS=clang
export BENCHES_TO_RUN=ibd
export IBD_CHECKPOINTS=2_000,10_000,20_000,80_000,140_000,300_000
export NO_CLEAN=1
export NO_TEARDOWN=1
export LOG_LEVEL=DEBUG

# Set minimumchainwork low so that we actually latch out of IBD at low
# heights.
export SYNCED_BITCOIND_ARGS=-minimumchainwork=0x00

if [ ! -f "/tmp/bitcoinperf-workdir" ]; then
  export BENCHES_TO_RUN=gitclone,build
  bitcoinperf --no-caution=1 --commits=master | tee perf.out
  grep "leaving workdir at" perf.out | grep -Eo "(/tmp/.*)" > /tmp/bitcoinperf-workdir
  rm perf.out
else
  bitcoinperf --no-caution=1 --commits "master" --workdir "$(cat /tmp/bitcoinperf-workdir)" "$@"
fi
