At the moment, we're running on sqlite. In order to use a Grafana frontend
for this data, we need to migrate to postgresql.


# Prepare the database

Provision a postgresql SQL instance in Google Cloud. 

The old sqlite has some bad data (since sqlite doesn't do length validation),
so we need to clean it up before inserting to postgres.

```
DELETE FROM codespeed_branch WHERE ID=5;
DELETE FROM codespeed_revision WHERE branch_id=5;
DELETE FROM codespeed_result WHERE revision_id=18;
```

# Test psql connection

Ensure we can reach the postgres target from our migration environment.

```
nc -vz psqlhost 5432
```

# Prepare the migrator

```
export DB_PASSWORD=""
export DB_HOST=""
gem install pg sqlite3 sequel
sequel -C sqlite:///data/data.db postgres://codespeed:$DB_PASSWORD@$DB_HOST/codespeed
```
