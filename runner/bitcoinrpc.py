import json

from . import sh


def call_rpc(cfg, cmd, on_synced=False):
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
