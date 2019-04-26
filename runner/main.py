#!/usr/bin/env python3
"""
Run a series of benchmarks against a particular Bitcoin Core revision(s).

See bin/runlocal.sh for a sample invocation.

"""
import atexit
import os
import subprocess
import tempfile
import datetime
import shlex
import getpass
import traceback
from pathlib import Path

from . import output, config, bitcoind, results, slack, benchmarks
from .logging import get_logger
from .sh import run
from .globals import G_, GitCheckout

logger = get_logger()


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


def run_benches(cfg):
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    logger.info(
        "Running benchmarks %s with compilers %s",
        cfg.benches_to_run, cfg.compilers)

    _startup_assertions(cfg)

    G_.workdir = Path(
        cfg.workdir or tempfile.mkdtemp(prefix=cfg.bench_prefix))

    benchmarks.bench_gitclone(cfg, G_.workdir / 'bitcoin')

    for remote, commit in config.get_commits(cfg):
        if commit != 'HEAD':
            if remote:
                run("git remote add {} https://github.com/{}/bitcoin.git"
                    .format(remote, remote), check_returncode=False)
                run("git fetch {}".format(remote))

            run("git checkout {}".format(commit))

        gitsha = subprocess.check_output(
            shlex.split('git rev-parse HEAD')).strip().decode()

        G_.gitco = GitCheckout(
            sha=gitsha, ref=commit, branch=cfg.repo_branch)
        logger.info("Checked out {}".format(G_.gitco))

        for compiler in cfg.compilers:
            G_.compiler = compiler
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
    G_.lockfile_acquired = True
    return True


def _get_shutdown_handler(should_teardown: bool):
    def handler():
        for node in bitcoind.Node.all_instances:
            if node.ps and node.ps.returncode is None:
                node.terminate()
                node.join()

        # Release lockfile if we've got it
        if G_.lockfile_acquired:
            LOCKFILE_PATH.unlink()
            logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

        # Clean up to avoid filling disk
        if G_.workdir and should_teardown and G_.workdir.is_dir():
            os.chdir(str(G_.workdir / ".."))
            _stash_debug_file()
            run("rm -rf %s" % G_.workdir)
            logger.debug("shutdown: removed workdir at %s", G_.workdir)
        elif not should_teardown:
            logger.debug("shutdown: leaving workdir at %s", G_.workdir)

    return handler


def _stash_debug_file():
    # Move the debug.log file out into /tmp for diagnostics.
    debug_file = G_.workdir / "data/debug.log"
    if debug_file.is_file():
        # Overwrite the file so as not to fill up disk.
        debug_file.rename(Path("/tmp/bench-debug.log"))


def main():
    cfg = config.parse_args()

    results.reporters.append(results.LogReporter())

    if cfg.codespeed_reporter:
        results.reporters.append(cfg.codespeed_reporter)

    if cfg.slack_client:
        slack.attach_slack_handler_to_logger(cfg.slack_client, logger)

    cfg.build_cache_path = None
    if cfg.use_build_cache:
        cfg.build_cache_path = Path.home() / '.bitcoinperf'
        cfg.build_cache_path.mkdir(exist_ok=True)

    atexit.register(_get_shutdown_handler(not cfg.no_teardown))

    logger.info("Running with configuration:")
    logger.info("")
    for name, val in sorted(cfg.__dict__.items()):
        logger.info("  {0:<26} {1:<40}".format(name, str(val)))
    logger.info("")

    try:
        run_benches(cfg)

        if len(config.get_commits(cfg)) <= 1:
            timestr = output.get_times_table(
                results.REF_TO_NAME_TO_TIME[G_.gitco.ref])
            print(timestr)
        else:
            print(dict(results.REF_TO_NAME_TO_TIME))
            output.print_comparative_times_table(results.REF_TO_NAME_TO_TIME)
            output.make_plots(G_.workdir.name, results.REF_TO_NAME_TO_TIME)
    except Exception:
        cfg.slack_client.send_to_slack_attachment(
            G_.gitco, "Error", {}, text=traceback.format_exc(), success=False)
        raise


if __name__ == '__main__':
    main()
