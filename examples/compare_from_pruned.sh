#!/usr/bin/env bash

set -ueo pipefail


if [ -z "$1" ]; then
  echo "Usage: <branch-to-benchmark> [<height-increase> <base-branch>]"
  echo
  echo "Examples: "
  echo
  echo "  ./compare_from_pruned.sh jamesob:2018-05-threadnames-take-2 1000 master"
  echo "  ./compare_from_pruned.sh v0.17.1"
  echo
fi

BRANCH_TO_BENCH="${1}"
HEIGHT_INCREASE="${2:-4000}"
BASE_BRANCH="${3:-master}"

# **you probably have to modify this** 
#
# This points to an already-synced datadir which the peer serving the other
# (under-benchmark) peer will use.
#
export SYNCED_DATADIR="/data/bitcoin_bench"

# **you probably have to modify this** 
#
# This points to the (already built) source tree that will run the serving
# peer.
#
export SYNCED_BITCOIN_REPO_DIR="${HOME}/src/bitcoin_bench"

# **you probably have to modify this** 
#
# This points to a pruned datadir starting at some height that the node being
# benchmarked will copy its datadir from.
#
export COPY_FROM_DATADIR="/data/bitcoin_pruned_500k"
PRUNED_STARTING_HEIGHT="500000"

# Set prune ridiculously high so that we don't actually prune, but are able to
# load in a chainstate that was previously pruned without having to reindex.
export CLIENT_BITCOIND_ARGS="-prune=1000000"
 
# These are recognized bitcoinperf envvars (see runner/config.py).
#
export COMMITS="${BASE_BRANCH},${BRANCH_TO_BENCH}"
export IBD_CHECKPOINTS="$(( PRUNED_STARTING_HEIGHT + HEIGHT_INCREASE ))"
# Speed up build time
export MAKE_JOBS="$(nproc --ignore=1)"


bitcoinperf \
  --ibd-peer-address localhost \
  --run-counts ibd:3 \
  --benches-to-run gitclone,build,ibd \
  --compilers clang \
  --no-caution 1 \
  --use-build-cache 1
