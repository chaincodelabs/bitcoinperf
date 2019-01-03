#!/bin/bash

set -e

if [ -z "$1" ]; then
  echo "Usage: ./backup_dashboard_json.sh <grafana api token>"
  exit 1
fi

declare -A dashboards
dashboards=(
  [overview]=YiV16Vsik
)

for name in "${!dashboards[@]}"; do
  uid="${dashboards[$name]}"
  curl -sL http://api_key:"$1"@bitcoinperf.com/api/dashboards/uid/"${uid}" \
    > backups/"${name}.json"
  echo "Saved $name ($uid)"
done
