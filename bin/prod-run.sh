#!/usr/bin/env bash

#
# Runs the bitcoinperf benchmarking process on our bench servers.
#

if ! python3.8 --version >/dev/null; then
  echo "Need to install Python 3.8"
  exit 1
fi

if ! grep bitcoinperf setup.py >/dev/null; then
  echo "Must run from the root of the bitcoinperf directory."
  exit 1
fi

BITCOINPERF_RUN_YML=${BITCOINPERF_RUN_YML:-examples/prod.yml}

git pull
python3.8 -m pip install -q --user --upgrade -e .

while true; do 
  sudo swapoff -a
  source runner/.env
  git pull
  ~/.local/bin/bitcoinperf run $BITCOINPERF_RUN_YML
  sleep 60
done
