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
import pickle
from pathlib import Path

from . import (
    output, config, bitcoind, results, slack, benchmarks, logging, git, sh)
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
        if cfg.safety_checks:
            raise RuntimeError(msg)
        else:
            logger.warning(msg)

    if run("pgrep --list-name bitcoin | grep -v bitcoinperf",
            check_returncode=False)[2] == 0:
        warn("benchmarks shouldn't run concurrently with unrelated bitcoin "
             "processes")

    if cfg.safety_checks:
        run('sudo swapoff -a')

    if run('cat /proc/swaps | grep -v "^Filename"',
            check_returncode=False)[2] != 1:
        warn("swap should be disabled during benchmarking")

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
        [i[0] for i in cfg.benches if i[1]], cfg.compilers)

    _startup_assertions(cfg)

    for target in cfg.to_bench:
        os.chdir(cfg.workdir)
        if (cfg.workdir / 'bitcoin').exists():
            sh.rm(cfg.workdir / 'bitcoin')

        G.gitco = git.checkout_in_dir(
            cfg,
            target,
            cfg.workdir / 'bitcoin',
            # TODO: pass copy_from_path
        )

        for compiler in cfg.compilers:
            G.compiler = compiler

            maybe_run_bench_some_times(
                target, cfg,
                cfg.benches.build, benchmarks.Build, always_run=True)

            maybe_run_bench_some_times(
                target, cfg, cfg.benches.unittests, benchmarks.MakeCheck)

            maybe_run_bench_some_times(
                target, cfg, cfg.benches.functests, benchmarks.FunctionalTests)

            maybe_run_bench_some_times(
                target, cfg, cfg.benches.microbench, benchmarks.Microbench)

        # Only do the following for gcc (since they're expensive)

        maybe_run_bench_some_times(
            target, cfg, cfg.benches.ibd_from_network, benchmarks.IbdReal)

        maybe_run_bench_some_times(
            target, cfg, cfg.benches.ibd_from_local, benchmarks.IbdLocal)

        maybe_run_bench_some_times(
            target, cfg,
            cfg.benches.ibd_range_from_local, benchmarks.IbdRangeLocal)

        maybe_run_bench_some_times(
            target, cfg, cfg.benches.reindex, benchmarks.Reindex)

        maybe_run_bench_some_times(
            target, cfg,
            cfg.benches.reindex_chainstate, benchmarks.ReindexChainstate)


def maybe_run_bench_some_times(
        target, cfg, bench_cfg, bench_class, *, always_run=False):
    if not bench_cfg and not always_run:
        logger.info("[%s] skipping benchmark", bench_class.name)
        return
    elif not bench_cfg:
        bench_cfg = config.BenchBuild()

    for i in range(getattr(bench_cfg, 'run_count', 1)):
        b = bench_class(cfg, bench_cfg, target, i)
        results.ALL_RUNS.append(b)
        b.wrapped_run(cfg, bench_cfg)


def _try_acquire_lockfile():
    if LOCKFILE_PATH.exists():
        return False

    with LOCKFILE_PATH.open('w') as f:
        f.write("%s,%s" % (datetime.datetime.utcnow(), getpass.getuser()))
    G.lockfile_acquired = True
    return True


def _get_shutdown_handler(cfg: config.Config):
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
        if cfg.teardown and cfg.workdir.is_dir():
            os.chdir(cfg.workdir)
            _stash_debug_file(cfg)

            # For now only remove the bitcoin subdir, since that'll be far and
            # away the biggest subdir.
            run("rm -rf %s" % (cfg.workdir / 'bitcoin'))
            logger.debug("shutdown: removed bitcoin dir at %s", cfg.workdir)
        elif not cfg.teardown:
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
    arg = sys.argv[1]
    cfg = None

    if arg.endswith('yml') or arg.endswith('yaml'):
        config_file = Path(sys.argv[1])
        if not config_file.exists():
            print(".yaml config file required as only argument",
                  file=sys.stderr)
            sys.exit(1)

        cfg = config.load(config_file)
        logging.configure_logger(cfg)

        if cfg.codespeed:
            results.Reporters.codespeed = results.CodespeedReporter(
                cfg.codespeed)

        G.slack = slack.Client(cfg.slack.webhook_url if cfg.slack else '')
        slack.attach_slack_handler_to_logger(cfg, G.slack, logger)

        atexit.register(_get_shutdown_handler(cfg))

        logger.info("Started on host %s (codespeed env %s)",
                    config.HOSTNAME, config.get_envname())
        logger.info(cfg.to_string(pretty=True))

        try:
            run_benches(cfg)
        except Exception:
            G.slack.send_to_slack_attachment(
                G.gitco, "Error", {},
                text=traceback.format_exc(), success=False)
            raise

        try:
            (cfg.results_dir / 'all_runs.pickle').write_bytes(pickle.dumps(
                results.ALL_RUNS))
            logger.info("Wrote serialized benchmark results to %s",
                        cfg.results_dir / 'all_runs.pickle')
        except Exception:
            logger.exception("failed to pickle results")

    elif arg.endswith('pickle'):
        results.ALL_RUNS = pickle.loads(Path(arg).read_bytes())

    grouped = output.GroupedRuns.from_list(results.ALL_RUNS)

    if not cfg:
        cfg = list(list(grouped.values())[0].values())[0][0].cfg

    if len(cfg.to_bench) <= 1:
        timestr = output.get_times_table(grouped)
        print(timestr)
    else:
        output.print_comparative_times_table(cfg, grouped)
        output.make_plots(cfg, grouped)


if __name__ == '__main__':
    main()
