#!/usr/bin/env bash

#
# Runs the bitcoinperf benchmarking process on our bench servers.
#

if ! python3.7 --version >/dev/null; then
  echo "Need to install Python 3.7"
  exit 1
fi

if ! grep bitcoinperf setup.py >/dev/null; then
  echo "Must run from the root of the bitcoinperf directory."
  exit 1
fi

git pull
python3.7 -m pip install -q --user --upgrade -e .

while true; do 
  sudo swapoff -a
  source runner/.env
  git pull
  ~/.local/bin/bitcoinperf examples/prod.yml
  sleep 60
done
