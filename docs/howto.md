

## Changing the Grafana `admin` user password

1. SSH to bitcoinperf.com
1. `sudo su bitcoinperf`
1. `cd ~/bitcoinperf`
1. Run `docker exec -u 0 -it $(docker ps | grep grafana/grafana | cut -f1 -d' ') bash`
   to shell into the running Grafana container.
1. On the container, run `grafana-cli admin reset-admin-password [new password]`.
