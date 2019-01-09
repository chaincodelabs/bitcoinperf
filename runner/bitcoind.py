import json
import time
import contextlib
import typing as t

from . import sh, config


def call_rpc(cfg, cmd, on_synced=False) -> t.Optional[dict]:
    """
    Call some bitcoin RPC command and return its deserialized output.
    """
    repodir = cfg.run_data.workdir / "bitcoin"
    datadir = repodir / "data"

    if on_synced:
        repodir = cfg.synced_bitcoin_repo_dir
        datadir = cfg.synced_data_dir

    info_call = sh.run(
        "{}/src/bitcoin-cli -datadir={} {}".format(repodir, datadir, cmd),
        check_returncode=False)

    if info_call[2] == 0:
        return json.loads(info_call[0].decode())

    cfg.logger.debug(
        "non-zero returncode (%s) from synced bitcoind status check",
        info_call[2])
    return None


@contextlib.contextmanager
def run_synced_bitcoind(cfg):
    """
    Context manager which spawns (and cleans up) a bitcoind instance that has a
    synced chain high enough to service an IBD up to BITCOIND_STOPATHEIGHT.
    """
    if not cfg.running_synced_bitcoind_locally:
        # If we're not running a node locally, don't worry about setup and
        # teardown.
        yield
        return

    bitcoinps = sh.popen(
        # Relies on bitcoind being precompiled and synced chain data existing
        # in /bitcoin_data; see runner/Dockerfile.
        "%s/src/bitcoind -datadir=%s -noconnect -listen=1 %s %s" % (
            cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir,
            config.BENCH_SPECIFIC_BITCOIND_ARGS, cfg.synced_bitcoind_args,
            ))

    cfg.logger.info(
        "started synced node with '%s' (pid %s)",
        bitcoinps.args, bitcoinps.pid)

    # Wait for bitcoind to come up.
    num_tries = 100
    sleep_time_secs = 2
    bitcoind_up = False

    def stop_synced_bitcoind():
        sh.run("{}/src/bitcoin-cli -datadir={} stop".format(
            cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir))
        bitcoinps.wait(timeout=120)

    while num_tries > 0 and bitcoinps.returncode is None and not bitcoind_up:
        info = call_rpc(cfg, "getblockchaininfo", on_synced=True)
        info_call = sh.run(
            "{}/src/bitcoin-cli -datadir={} getblockchaininfo".format(
                cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir),
            check_returncode=False)

        if info_call[2] == 0:
            info = json.loads(info_call[0].decode())
        else:
            cfg.logger.debug(
                "non-zero returncode (%s) from synced bitcoind status check",
                info_call[2])

        if info and info["blocks"] < int(cfg.bitcoind_stopatheight):
            stop_synced_bitcoind()  # Stop process; we're exiting.
            raise RuntimeError(
                "synced bitcoind node doesn't have enough blocks "
                "(%s vs. %s)" %
                (info['blocks'], int(cfg.bitcoind_stopatheight)))
        elif info:
            bitcoind_up = True
        else:
            num_tries -= 1
            time.sleep(sleep_time_secs)

    if not bitcoind_up:
        raise RuntimeError("Couldn't bring synced node up")

    cfg.logger.info("synced node is active (pid %s) %s", bitcoinps.pid, info)

    try:
        yield
    finally:
        cfg.logger.info("shutting down synced node (pid %s)", bitcoinps.pid)
        stop_synced_bitcoind()

        if bitcoinps.returncode != 0:
            cfg.logger.warning(
                "synced bitcoind returned with nonzero return code "
                "%s" % bitcoinps.returncode)
