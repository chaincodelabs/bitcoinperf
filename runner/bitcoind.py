import json
import time
import contextlib
import typing as t
from pathlib import Path

from . import sh, config, logging

logger = logging.get_logger()


def call_rpc(cfg, cmd,
             on_synced=False,
             deserialize_output=True,
             quiet=False,
             ) -> t.Optional[dict]:
    """
    Call some bitcoin RPC command and return its deserialized output.
    """
    repodir = cfg.run_data.src_dir
    datadir = cfg.run_data.data_dir
    rpcport = cfg.bitcoind_rpcport
    extra_args = "-rpcuser=foo -rpcpassword=bar"

    if on_synced:
        repodir = cfg.synced_bitcoin_repo_dir
        datadir = cfg.synced_data_dir
        rpcport = cfg.synced_bitcoind_rpcport
        extra_args = ""

    info_call = sh.run(
        "{}/src/bitcoin-cli -rpcport={} -datadir={} {} {}".format(
            repodir, rpcport, datadir, extra_args, cmd),
        check_returncode=False)

    if info_call[2] != 0:
        logger.debug(
            "non-zero returncode from synced bitcoind status check: %s",
            info_call)
        return None

    if not deserialize_output:
        logger.info("rpc: %r -> %r", cmd, info_call[0])
    else:
        logger.debug("response for %r:\n%s",
                     cmd, json.loads(info_call[0].decode()))

    return json.loads(info_call[0].decode()) if deserialize_output else None


def stop_via_rpc(cfg, ps, on_synced=False):
    logger.info("Calling stop on bitcoind ps %s", ps)
    call_rpc(cfg, "stop", on_synced=on_synced, deserialize_output=False)
    ps.wait(timeout=120)


def empty_datadir(bitcoin_src_dir: Path):
    """Ensure empty data before each IBD."""
    datadir = bitcoin_src_dir / 'data'
    sh.run("rm -rf %s" % datadir, check_returncode=False)
    if not datadir.exists():
        datadir.mkdir()


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
        "%s/src/bitcoind -rpcport=%s -datadir=%s -noconnect -listen=1 %s %s"
        % (
            cfg.synced_bitcoin_repo_dir, cfg.synced_bitcoind_rpcport,
            cfg.synced_data_dir,
            config.BENCH_SPECIFIC_BITCOIND_ARGS, cfg.synced_bitcoind_args,
            ))

    logger.info(
        "started synced node with '%s' (pid %s)",
        bitcoinps.args, bitcoinps.pid)

    # Wait for bitcoind to come up.
    num_tries = 100
    sleep_time_secs = 2
    bitcoind_up = False

    while num_tries > 0 and bitcoinps.returncode is None and not bitcoind_up:
        info = call_rpc(cfg, "getblockchaininfo", on_synced=True)
        info_call = sh.run(
            "{}/src/bitcoin-cli -datadir={} getblockchaininfo".format(
                cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir),
            check_returncode=False)

        if info_call[2] == 0:
            info = json.loads(info_call[0].decode())
        else:
            logger.debug(
                "non-zero returncode (%s) from synced bitcoind status check",
                info_call[2])

        if info and info["blocks"] < int(cfg.bitcoind_stopatheight):
            # Stop process; we're exiting.
            stop_via_rpc(cfg, bitcoinps, on_synced=True)
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

    logger.info("synced node is active (pid %s) %s", bitcoinps.pid, info)

    try:
        yield
    finally:
        logger.info("shutting down synced node (pid %s)", bitcoinps.pid)
        stop_via_rpc(cfg, bitcoinps, on_synced=True)

        if bitcoinps.returncode != 0:
            logger.warning(
                "synced bitcoind returned with nonzero return code "
                "%s" % bitcoinps.returncode)
