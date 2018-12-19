## Migrating to psql and grafana

At the moment, we're running on sqlite. In order to use a Grafana frontend for
this data (and ditch the ugly codespeed interface), we need to migrate to
postgresql. During this migration, we'll also move to a standard docker-compose
based runtime so that development and production are more similar (and
management easier).

For this migration, I'll be standing up a new server in GCP (called
"bitcoinperf") to replace the old server (called "codespeed"). Once the
migration is complete, I'll change the DNS records for bitcoinperf.com to point
to the new server.

### Deploy the new code

Provision a new server on GCP running the `psql-grafana` branch of bitcoinperf.
Ensure docker-compose has started both codespeed and grafana containers. Ensure
both are accessible at https://codespeed.bitcoinperf.com and
https://grafana.bitcoinperf.com.


### Prepare the database

Provision a postgresql SQL instance in Google Cloud. 

The old sqlite has some bad data (since sqlite doesn't do length validation),
so we need to clean it up before inserting to postgres.

```sh
bitcoinperf$ sudo su bitcoinperf
bitcoinperf$ scp james@codespeed:bitcoin-perfmonitor/codespeed/bitcoin_codespeed/data.db data.db
bitcoinperf$ sqlite data.db

DELETE FROM codespeed_branch WHERE ID=5;
DELETE FROM codespeed_revision WHERE branch_id=5;
DELETE FROM codespeed_result WHERE revision_id=18;
```

### Test psql connection

Ensure we can reach the postgres target from our migration environment.

```
bitcoinperf $ nc -vz 35.231.106.7 5432
```

### Prepare the migrator

We use a program called `sequel` to migrate the sqlite database into a postgres
connection.

```sh
bitcoinperf$ cd bitcoinperf
bitcoinperf$ docker-compose build sqlmigrate
bitcoinperf$ docker-compose run --rm sqlmigrate /bin/bash

sqlmigrate$ export DB_PASSWORD=""
sqlmigrate$ export DB_HOST=""
sqlmigrate$ gem install pg sqlite3 sequel
sqlmigrate$ sequel -C sqlite:///data/data.db postgres://codespeed:$DB_PASSWORD@$DB_HOST/codespeed
```
