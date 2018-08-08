## Bitcoin Core performance monitor 📈

This repository consists of two components

- a [codespeed](https://github.com/chaincodelabs/codespeed) installation which
  collects and presents benchmarking results in a web interface, and
- a haphazard Python script for running high-level Bitcoin Core benchmarks which
  POSTs results to codespeed.

The benchmarks which are monitored are

- Build time (make)
- Unittest duration (make check)
- Functional test framework duration (test/functional/test_runner.py)
- Microbenchmarks (bench-bitcoin)
- IBD up to some height from a local peer or from the P2P network
- Reindex up to some height


### Testing

Install docker & docker-compose, then run

```sh
$ docker-compose build
$ docker-compose up
```

and a benchmark up to height 10,000 will be run in test containers.

### Installation

0. Obtain all the dependencies necessary to build Bitcoin Core as well as all
   additional depedencies (see `runner/provision`). Obtain an up-to-date
   copy of the chain at some `$datadir` location.
0. Then, run `pip3 install -r runner/requirements.txt`.

#### Starting codespeed

0. `cd codespeed && pip install --user -r requirements.txt`
0. Initialize the codespeed DB: `python manage.py migrate`
0. Create an admin user (for posting results): `python manage.py createsuperuser`
0. Load required initial data:
   `python manage.py shell < ./codespeed/initialize_data.py`
0. In a separate terminal window, start the development server: `python
   manage.py runserver 0.0.0.0:8000`
0. Browse to http://localhost:8000 and ensure codespeed is up.


#### Starting the synced peer

0. [assuming you have obtained a relatively up-to-date chain in `$datadir`]
0. In a separate terminal window, run `./bin/start_synced $datadir`
0. Ensure the peer is up by running
   `/path/to/bitcoin-cli -rpcport=9001 -datadir="${datadir}" getblockchaininfo`.


#### Running the benchmarks

0. Run `./bin/run_bench`.


### Running a subset of benches

Use the `BENCHES_TO_RUN` envvar when invoking `runner/run_bench.py` to only
run certain benchmarks.

### Running to a height

Use the `BITCOIND_STOPATHEIGHT` envvar when invoking `runner/run_bench.py` to
control the height to sync to. This will automatically be reflected in the name
of the benchmarks which are generated.
