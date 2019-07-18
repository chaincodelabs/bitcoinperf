#!/usr/bin/env bash

# Run this as bitcoinperf@bitcoinperf.com to obtain a postgres shell.

cd /home/bitcoinperf/bitcoinperf
DBURL=$(cat .env | grep DATABASE_URL | cut -d= -f2)
docker run -e DATABASE_URL=${DBURL} --rm -it python:3.6.3 \
  /bin/bash -c 'pip install pgcli; pgcli $DATABASE_URL'
