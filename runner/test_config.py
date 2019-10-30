import shutil
from pathlib import Path

import yaml
import pytest

from . import config


@pytest.fixture
def setup_files():
    for fake_datadir in [
            Path('/tmp/bitcoin_pruned_500k'),
    ]:
        (fake_datadir / 'blocks').mkdir(parents=True)
        (fake_datadir / 'chainstate').mkdir(parents=True)

    for fake_src in [
            Path('/tmp/bitcoin'),
    ]:
        (fake_src / 'src').mkdir(parents=True)
        (fake_src / 'src' / 'bitcoin-cli').touch()
        (fake_src / 'src' / 'bitcoind').touch()

    yield

    shutil.rmtree('/tmp/bitcoin_pruned_500k')
    shutil.rmtree('/tmp/bitcoin')


def test_parse_config(setup_files):
    c = config.Config(**yaml.load(TEST_CFG, Loader=yaml.Loader))

    assert [i[0] for i in c.benches] == [
        'unittests',
        'functests',
        'microbench',
        'ibd_from_network',
        'ibd_from_local',
        'ibd_range_from_local',
        'reindex',
        'reindex_chainstate',
    ]

    assert c.benches.functests.num_jobs == 4
    assert c.benches.functests.run_count == 3

    assert c.benches.ibd_from_network.start_height == 0
    assert c.benches.ibd_from_network.run_count == 1
    assert c.benches.ibd_from_network.time_heights[1] == 505_000

    assert c.to_bench[0].gitref == 'master'
    assert c.to_bench[0].bitcoind_extra_args == '-logthreadnames'

    assert c.to_bench[1].gitref == 'fad88bd6c9f85e6e7f8fb66a94aa75c67d26b7d8'
    assert c.to_bench[1].bitcoind_extra_args == '-logthreadnames'

    assert c.to_bench[2].gitref == '1905-buildStackReuseNone'
    assert c.to_bench[2].gitremote == 'MarcoFalke'
    assert c.to_bench[2].bitcoind_extra_args == ''


TEST_CFG = """
---

# Where various data output is dumped
artifact_dir: /tmp/output

# Cache a built bitcoin src directory and restore it from the cache on
# subsequent runs.
cache_build: true

# Set to false to make cache dropping optional and bypass various safety checks.
safety_checks: true

synced_peer:
  datadir: /tmp/bitcoin_pruned_500k
  repodir: /tmp/bitcoin

  # or, if over network
  #
  # address:

benches:

  unittests:
    enabled: true
    num_jobs: 4

  functests:
    enabled: false
    num_jobs: 4
    run_count: 3

  microbench:
    # benches: this,that,theother

  ibd_from_network:
    end_height: 522_000
    time_heights:
      - 500_000
      - 505_000

  ibd_from_local:
    end_height: 522_000
    stash_datadir: /tmp/datadir
    time_heights:
      - 500_000

  ibd_range_from_local:
    start_height: 500_000
    end_height: 505_000
    src_datadir: /tmp/bitcoin_pruned_500k
    time_heights:
      - 500_000

  reindex:
    enabled: false
    time_heights:
      - 500_000

  reindex_chainstate:
    enabled: false


to_bench:

  - gitref: master
    bitcoind_extra_args: "-logthreadnames"

  - gitref: fad88bd6c9f85e6e7f8fb66a94aa75c67d26b7d8
    bitcoind_extra_args: "-logthreadnames"

  - gitref: 1905-buildStackReuseNone
    gitremote: MarcoFalke
"""
