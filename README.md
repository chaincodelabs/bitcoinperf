## bitcoinperf

[bitcoinperf.com](https://bitcoinperf.com)
 
![nice things](./img/ibd.png)
              
---

## tl;dr I want to bench a PR 

This should probably be done on an otherwise idle system if you actually want
to report the benchmarks.

1. Clone this repo: `git clone https://github.com/chaincodelabs/bitcoinperf.git`
1. Ensure you have Python 3.8 on your system (see `./bin/install.sh`).
1. Peruse all the other stuff you should have installed in `./bin/install.sh`.
1. `python3 -m pip install --user -e .`
1. Follow all setup instructions: `bitcoinperf setup`
1. Run the bench: `bitcoinperf bench-pr $YOUR_PR_NUM`
  - This will run a comparative initial block download from block 500_000 to
    (by default) block 501_000. It will spit out some pretty graphs and
    statistics.

---

This repository consists of a few components

- a haphazard Python script for running high-level Bitcoin Core benchmarks,
- a [codespeed](https://github.com/chaincodelabs/codespeed) installation which
  collects and presents benchmarking results in a web interface, and
- a Grafana interface for presenting the benchmark results.

*It even uses matplotlib to generate graphs that look decent half the time.*

The benchmarks which are monitored are

- Build time (make)
- Unittest duration (make check)
- Functional test framework duration (test/functional/test_runner.py)
- Microbenchmarks (bench-bitcoin)
- IBD up to some height from a local peer or from the P2P network
- IBD of an interesting range of the chain (based on preexisting datadir)
- Reindex up to some height

The Python script (`bitcoinperf`) may be used as a standalone script
(in conjunction with the Docker configuration) to benchmark and compare
different Bitcoin commits locally - without necessarily writing to a remote
codespeed instance.

### Example local usage (no docker)

You must have Python 3.8 or greater installed.

```sh
# Obtain all the dependencies necessary to build Bitcoin Core as well as all
# additional dependencies.
#
# This script is written for Debian-like systems - if you're not on one of
# those, take a look at the script. It should be pretty obvious what you need to
# do.
./bin/install.sh
# If pip warns that the installation path is not in PATH, add it
export PATH=$PATH:~/.local/bin

# Run the guided setup script
bitcoinperf setup

# To run a probably-relevant comparison for a certain pull request, run
bitcoinperf bench-pr $PR_NUM

# To run based upon YAML configuration, use
bitcoinperf run examples/pr_compare.yml
```

See the [examples/](examples/) for sample usages.


### Example local usage (docker)

First, you may have to modify the `synced` mountpoint in `docker-compose.yml`
from `/data/bitcoin_bench` to a path on your machine that corresponds to a
Bitcoin datadir which is synced up to your desired stopatheight.

Install docker & docker-compose, then run

#
```sh
# Bring up codespeed server and a synced bitcoind instance

$ ./bin/dev up codespeed

# Modify docker-compose.yml to reference a synced datadir on your host machine.

$ sed -ie 's#/data/bitcoin_bench#/path/to/your/datadir#g' docker-compose.dev.yml

$ ./bin/dev runbench bitcoinperf run examples/smoketest.yml
```

Navigate to http://localhost:8000/ to see results reported to codespeed.

### Running unittests

```sh
$ ./bin/dev up codespeed
$ ./bin/dev test
```

### Configuring Grafana

Grafana dashboards can be recreated locally by importing the JSON files
stored in `grafana_management/backups/`.

When dashboards are edited on the live environment, they should be backed up
using `grafana_management/backup_dashboard_json.sh`.

In order for the saved Grafana dashboard configurations to work, you'll need
to make sure you've installed the Postgres views contained in
`codespeed/migrations/001-result-views.sql`.
