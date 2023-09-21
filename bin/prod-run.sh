#!/usr/bin/env bash

#
# Runs the bitcoinperf benchmarking process on our bench servers.
#
# Environment variables:
#
#   - BITCOINPERF_RUN_YML: specify which yaml script to execute
#     (defaults to ./runner/prod.yml)
#

if ! grep bitcoinperf setup.py >/dev/null; then
  echo "Must run from the root of the bitcoinperf directory."
  exit 1
fi

git pull
python3.8 -m pip install -q --user --upgrade -e .

tries=3

# File that keeps environment-specific config and credentials
PROD_ENV_FILE=./runner/.env

# Which YAML file to run.
YAML=${BITCOINPERF_RUN_YML:-./examples/prod.yml}

while [ $tries -gt 0 ]; do
  sudo swapoff -a
  git pull

  if [ -f $PROD_ENV_FILE ]; then
    source $PROD_ENV_FILE
  else
    echo "warning: no production env file (${PROD_ENV_FILE}) to source"
  fi

  if ! ~/.local/bin/bitcoinperf run ${YAML} ; then
    # On failure, back off and decrement tries
    ((tries=tries-1))
    echo
    echo "*************************************************"
    echo "bitcoinperf run failed"
    echo "*************************************************"
    echo
    sleep 60
  else
    # Reset the counter each time we hit a success
    tries=3
  fi
done
