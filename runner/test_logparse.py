from . import logparse


def test_logparse(datadir):
    with open(datadir / 'bitcoind-flush.log', 'r') as f:
        assert logparse.get_flush_times(f) == [
            logparse.FlushEvent(9, 0.00, 1201, 276),
            logparse.FlushEvent(9, 0.00, 0, 11)]
