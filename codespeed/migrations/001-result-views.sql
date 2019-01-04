--
-- This file creates a number of views that make constructing queries 
-- on Grafana much easier.
--

CREATE OR REPLACE VIEW all_result AS
SELECT
  res.id as id,
  res.date as date,
  res.value as value,
  bench.name as bench_name,
  env.name as env_name,
  env.id as env_id,
  rev.commitid as commitid,
  rev.author as author
FROM codespeed_result as res
  INNER JOIN codespeed_benchmark as bench ON res.benchmark_id = bench.id
  INNER JOIN codespeed_environment as env ON res.environment_id = env.id
  INNER JOIN codespeed_revision as rev ON res.revision_id = rev.id;


CREATE OR REPLACE VIEW ibd_result AS
SELECT
  res.id as id,
  res.date as date,
  res.value as value,
  bench.name as bench_name,
  env.name as env_name,
  env.id as env_id,
  rev.commitid as commitid,
  rev.author as author,
  substring(bench.name from 'ibd.([a-zA-Z]+).') as type,
  substring(bench.name from 'local.([0-9]+).') as height,
  substring(bench.name from 'dbcache=([0-9]+)') as dbcache
FROM codespeed_result as res
INNER JOIN codespeed_benchmark as bench ON res.benchmark_id = bench.id
INNER JOIN codespeed_environment as env ON res.environment_id = env.id
INNER JOIN codespeed_revision as rev ON res.revision_id = rev.id
WHERE
  bench.name like 'ibd.%' AND bench.name not like '%.mem-usage';


CREATE OR REPLACE VIEW ibd_memusage_result AS
SELECT
  res.id as id,
  res.date as date,
  res.value as value,
  bench.name as bench_name,
  env.name as env_name,
  env.id as env_id,
  rev.commitid as commitid,
  rev.author as author,
  substring(bench.name from 'ibd.([a-zA-Z]+).') as type,
  substring(bench.name from 'local.([0-9]+).') as height,
  substring(bench.name from 'dbcache=([0-9]+)') as dbcache
FROM codespeed_result as res
INNER JOIN codespeed_benchmark as bench ON res.benchmark_id = bench.id
INNER JOIN codespeed_environment as env ON res.environment_id = env.id
INNER JOIN codespeed_revision as rev ON res.revision_id = rev.id
WHERE
  bench.name like 'ibd.%' AND bench.name like '%.mem-usage';


CREATE OR REPLACE VIEW reindex_result AS
SELECT
  res.id as id,
  res.date as date,
  res.value as value,
  bench.name as bench_name,
  env.name as env_name,
  env.id as env_id,
  rev.commitid as commitid,
  rev.author as author,
  substring(bench.name from 'reindex.([0-9]+).') as height,
  substring(bench.name from 'dbcache=([0-9]+)') as dbcache
FROM codespeed_result as res
INNER JOIN codespeed_benchmark as bench ON res.benchmark_id = bench.id
INNER JOIN codespeed_environment as env ON res.environment_id = env.id
INNER JOIN codespeed_revision as rev ON res.revision_id = rev.id
WHERE
  bench.name like 'reindex.%' AND bench.name not like '%.mem-usage';


CREATE OR REPLACE VIEW reindex_memusage_result AS
SELECT
  res.id as id,
  res.date as date,
  res.value as value,
  bench.name as bench_name,
  env.name as env_name,
  env.id as env_id,
  rev.commitid as commitid,
  rev.author as author,
  substring(bench.name from 'reindex.([0-9]+).') as height,
  substring(bench.name from 'dbcache=([0-9]+)') as dbcache
FROM codespeed_result as res
INNER JOIN codespeed_benchmark as bench ON res.benchmark_id = bench.id
INNER JOIN codespeed_environment as env ON res.environment_id = env.id
INNER JOIN codespeed_revision as rev ON res.revision_id = rev.id
WHERE
  bench.name like 'reindex.%' AND bench.name like '%.mem-usage';

CREATE USER grafanareader WITH PASSWORD 'password';
GRANT USAGE ON SCHEMA public TO grafanareader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafanareader;
