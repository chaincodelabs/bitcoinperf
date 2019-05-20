#!/usr/bin/env python3.7
# vim: ft=python
"""
Run a series of benchmarks against a particular Bitcoin Core revision(s).

See bin/runlocal.sh for a sample invocation.

"""
import atexit
import os
import datetime
import getpass
import traceback
import sys
import yaml
from pathlib import Path

from . import (
    output, config, bitcoind, results, slack, benchmarks, logging, git)
from .globals import G
from .logging import get_logger
from .sh import run

logger = get_logger()

assert sys.version_info >= (3,  7), "Python 3.7 required"

# Maintain a lockfile that is global across the host to ensure that we're not
# running more than one instance on a given system.
LOCKFILE_PATH = Path("/tmp/bitcoin_bench.lock")


def _startup_assertions(cfg):
    """
    Ensure the benchmark environment is suitable in various ways.
    """
    if run("$(which time) -f %M sleep 0.01",
            check_returncode=False)[2] != 0:
        raise RuntimeError("the time package is required")

    def warn(msg):
        if cfg.no_caution:
            logger.warning(msg)
        else:
            raise RuntimeError(msg)

    if run("pgrep --list-name bitcoin | grep -v bitcoinperf",
            check_returncode=False)[2] == 0:
        warn("benchmarks shouldn't run concurrently with unrelated bitcoin "
             "processes")

    if not cfg.no_caution:
        run('sudo swapoff -a')

    if run('cat /proc/swaps | grep -v "^Filename"',
            check_returncode=False)[2] != 1:
        warn("swap must be disabled during benchmarking")

    if not _try_acquire_lockfile():
        raise RuntimeError(
            "Couldn't acquire lockfile %s; exiting", LOCKFILE_PATH)

    synced_bitcoind_path = cfg.synced_bitcoin_repo_dir / 'src' / 'bitcoind'
    if not (synced_bitcoind_path.is_file() and
            os.access(synced_bitcoind_path, os.X_OK)):
        raise RuntimeError("bitcoind executable missing at {}".format(
            synced_bitcoind_path))

    if not cfg.copy_from_datadir.is_dir():
        raise RuntimeError("COPY_FROM_DATADIR doesn't exist ({})".format(
            cfg.copy_from_datadir))


def run_benches(cfg):
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    logger.info(
        "Running benchmarks %s with compilers %s",
        cfg.benches_to_run, cfg.compilers)

    _startup_assertions(cfg)

    for target in config.to_bench:
        cfg.current_git_co = git.checkout_in_dir(cfg.workdir / 'bitcoin')

        # TODO: run counts

        for compiler in cfg.compilers:
            cfg.current_compiler = compiler
            benchmarks.bench_build(cfg)
            benchmarks.bench_makecheck(cfg)
            benchmarks.bench_functests(cfg)
            benchmarks.bench_microbench(cfg)

        benchmarks.bench_ibd(cfg)


def _try_acquire_lockfile():
    if LOCKFILE_PATH.exists():
        return False

    with LOCKFILE_PATH.open('w') as f:
        f.write("%s,%s" % (datetime.datetime.utcnow(), getpass.getuser()))
    G.lockfile_acquired = True
    return True


def _get_shutdown_handler(cfg: config.Config, should_teardown: bool):
    def handler():
        for node in bitcoind.Node.all_instances:
            if node.ps and node.ps.returncode is None:
                node.terminate()
                node.join()

        # Release lockfile if we've got it
        if G.lockfile_acquired:
            LOCKFILE_PATH.unlink()
            logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

        # Clean up to avoid filling disk
        # TODO add more granular cleanup options
        if should_teardown and cfg.workdir.is_dir():
            os.chdir(cfg.workdir)
            _stash_debug_file(cfg)

            # For now only remove the bitcoin subdir, since that'll be far and
            # away the biggest subdir.
            run("rm -rf %s" % (cfg.workdir / 'bitcoin'))
            logger.debug("shutdown: removed bitcoin dir at %s", cfg.workdir)
        elif not should_teardown:
            logger.debug("shutdown: leaving bitcoin dir at %s", cfg.workdir)

    return handler


def _stash_debug_file(cfg: config.Config):
    """
    Throw the last debug file into /tmp so that we avoid removing it with the
    rest of the bitcoin stuff.
    """
    # Move the debug.log file out into /tmp for diagnostics.
    debug_file = cfg.workdir / 'bitcoin' / 'data' / 'debug.log'
    if debug_file.is_file():
        # Overwrite the file so as not to fill up disk.
        debug_file.rename(Path("/tmp/bitcoinperf-last-debug.log"))


def main():
    config_file = Path(sys.argv[1])
    if not config_file.exists():
        print(".yaml config file required as only argument",
              file=sys.stderr)
        sys.exit(1)

    cfg = config.Config(**yaml.load(config_file.read_text()))
    logging.configure_logger(cfg)

    if cfg.codespeed:
        results.Reporters.codespeed = results.CodespeedReporter(cfg.codespeed)

    if cfg.slack:
        slack.attach_slack_handler_to_logger(cfg.slack.get_client(), logger)

    atexit.register(_get_shutdown_handler(not cfg.no_teardown))

    logger.info("Running with configuration:")
    logger.info("")
    for name, val in sorted(cfg.__dict__.items()):
        logger.info("  {0:<26} {1:<40}".format(name, str(val)))
    logger.info("")

    try:
        run_benches(cfg)

        if len(cfg.to_bench) <= 1:
            timestr = output.get_times_table(
                results.REF_TO_NAME_TO_TIME[G.gitco.ref])
            print(timestr)
        else:
            print(dict(results.REF_TO_NAME_TO_TIME))
            output.print_comparative_times_table(results.REF_TO_NAME_TO_TIME)
            output.make_plots(cfg, results.REF_TO_NAME_TO_TIME)
    except Exception:
        cfg.slack_client.send_to_slack_attachment(
            G.gitco, "Error", {}, text=traceback.format_exc(), success=False)
        raise


if __name__ == '__main__':
    main()
