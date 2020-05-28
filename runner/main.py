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
import typing as t
from pathlib import Path

import clii

from . import (
    output, config, bitcoind, results, slack, benchmarks, logging, git, sh,
    hwinfo)
from .globals import G
from .logging import get_logger

logger = get_logger()

assert sys.version_info >= (3,  7), "Python 3.7 required"

# Maintain a lockfile that is global across the host to ensure that we're not
# running more than one instance on a given system.
LOCKFILE_PATH = Path("/tmp/bitcoin_bench.lock")


def _startup_assertions(cfg):
    """
    Ensure the benchmark environment is suitable in various ways.
    """
    if sh.run("$(which time) -f %M sleep 0.01", quiet=True).returncode != 0:
        raise RuntimeError("the time package is required")

    def warn(msg):
        if cfg.safety_checks:
            raise RuntimeError(msg)
        else:
            logger.warning(msg)

    if sh.run("pgrep --list-name bitcoin | grep -v bitcoinperf",
              quiet=True).returncode == 0:
        warn("benchmarks shouldn't run concurrently with unrelated bitcoin "
             "processes")

    if cfg.safety_checks:
        sh.run('sudo -n swapoff -a')

    if sh.run('cat /proc/swaps | grep -v "^Filename"',
              check=False).returncode != 1:
        warn("swap should be disabled during benchmarking")

    avg, _, _ = os.getloadavg()
    if avg > 1.:
        warn(f"1min load average high: {avg}")

    if not _try_acquire_lockfile():
        raise RuntimeError(
            "Couldn't acquire lockfile %s; exiting", LOCKFILE_PATH)


def _cleanup_tmpfiles():
    """Remove temporary bitcoinperf directories older than 3 days."""
    # TODO parameterize this
    sh.run(r'find /tmp/bitcoinperf-* -mtime +3 -exec rm -rf {} \;')
    sh.run(r'find /tmp/test_runner_* -mtime +3 -exec rm -rf {} \;')


def run_full_suite(cfg):
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    logger.info(
        "Running benchmarks %s with compilers %s",
        [i[0] for i in cfg.benches if i[1]], cfg.compilers)

    # TODO: move this somewhere more appropriate.
    _cleanup_tmpfiles()
    _startup_assertions(cfg)
    checkouts, bad_targets = git.resolve_targets(
        cfg.workdir / 'bitcoin', cfg.to_bench)

    if bad_targets:
        logger.warning("Couldn't resolve git targets: %s", bad_targets)
        return

    for target in cfg.to_bench:
        os.chdir(cfg.workdir)
        if (cfg.workdir / 'bitcoin').exists():
            sh.rm(cfg.workdir / 'bitcoin')

        assert target.gitco
        G.gitco = target.gitco

        for compiler in cfg.compilers:
            maybe_run_bench_some_times(
                target, cfg, compiler,
                cfg.benches.build, benchmarks.Build, always_run=True)

            maybe_run_bench_some_times(
                target, cfg, compiler,
                cfg.benches.unittests, benchmarks.MakeCheck)

            maybe_run_bench_some_times(
                target, cfg, compiler,
                cfg.benches.functests, benchmarks.FunctionalTests)

            maybe_run_bench_some_times(
                target, cfg, compiler,
                cfg.benches.microbench, benchmarks.Microbench)

        # Only do the following for gcc (since they're expensive)

        maybe_run_bench_some_times(
            target, cfg, compiler,
            cfg.benches.ibd_from_network, benchmarks.IbdReal)

        maybe_run_bench_some_times(
            target, cfg, compiler,
            cfg.benches.ibd_from_local, benchmarks.IbdLocal)

        maybe_run_bench_some_times(
            target, cfg, compiler,
            cfg.benches.ibd_range_from_local, benchmarks.IbdRangeLocal)

        maybe_run_bench_some_times(
            target, cfg, compiler,
            cfg.benches.reindex, benchmarks.Reindex)

        maybe_run_bench_some_times(
            target, cfg, compiler,
            cfg.benches.reindex_chainstate, benchmarks.ReindexChainstate)


def maybe_run_bench_some_times(
        target, cfg, compiler, bench_cfg, bench_class, *, always_run=False):
    if not bench_cfg and not always_run:
        logger.info("[%s] skipping benchmark", bench_class.name)
        return
    elif not bench_cfg:
        bench_cfg = config.BenchBuild()

    for i in range(getattr(bench_cfg, 'run_count', 1)):
        b = bench_class(cfg, bench_cfg, compiler, target, i)
        results.ALL_RUNS.append(b)
        b.run(cfg, bench_cfg)


def _try_acquire_lockfile():
    if LOCKFILE_PATH.exists():
        return False

    with LOCKFILE_PATH.open('w') as f:
        f.write("%s,%s" % (datetime.datetime.utcnow(), getpass.getuser()))
    G.lockfile_held = True
    return True


def _get_shutdown_handler(cfg: config.Config):
    def handler():
        for node in bitcoind.Node.all_instances:
            if node.ps and node.ps.returncode is None:
                node.terminate()
                node.join()

        # Release lockfile if we've got it
        if G.lockfile_held:
            LOCKFILE_PATH.unlink()
            logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

        # Clean up to avoid filling disk
        # TODO add more granular cleanup options
        if cfg.teardown and cfg.workdir.is_dir():
            os.chdir(cfg.workdir)
            _stash_debug_file(cfg)

            # For now only remove the bitcoin subdir, since that'll be far and
            # away the biggest subdir.
            sh.run("rm -rf %s" % (cfg.workdir / 'bitcoin'))
            logger.debug("shutdown: removed bitcoin dir at %s", cfg.workdir)
        elif not cfg.teardown:
            logger.debug("shutdown: leaving bitcoin dir at %s", cfg.workdir)

    return handler


def _stash_debug_file(cfg: config.Config):
    """
    Throw the last debug file so that we avoid removing it with the
    rest of the bitcoin stuff.
    """
    assert cfg.workdir
    # Move the debug.log file out into /tmp for diagnostics.
    debug_file = cfg.workdir / 'bitcoin' / 'data' / 'debug.log'
    if debug_file.is_file():
        # Overwrite the file so as not to fill up disk.
        debug_file.rename(cfg.workdir / 'stashed-debug.log')


cli = clii.App(description=__doc__)
cli.add_arg('--verbose', '-v', action='store_true')


@cli.cmd
def bench_pr(pr_num: str, run_id: str = None, server_tag: str = 'v0.20.0rc2'):
    """
    Args:
        server_tag: which git tag the server bitcoind process will run
    """
    run_id = run_id or pr_num
    workdir = Path(f'/tmp/bitcoinperf-{run_id}')
    if workdir.exists():
        sh.rm(workdir)
    workdir.mkdir()
    repodir = workdir / 'bitcoin'
    logging.configure_logger(workdir, 'DEBUG' if cli.args.verbose else 'INFO')

    targets = [
        config.Target(
            name=f"#{pr_num}", gitref=f'pr/{pr_num}', rebase=False),
        config.Target(
            name=git.MERGEBASE_REF, gitref=f'master', gitremote='origin',
            rebase=False),
    ]

    checkouts, bad_targets = git.resolve_targets(repodir, targets)
    if bad_targets:
        print(f"failed to find commit for {[t.gitref for t in bad_targets]}")
        sys.exit(1)

    end_height = 90_000

    peer = config.SyncedPeer(
        datadir=Path.home() / '.bitcoin',
        repodir=Path.home() / 'src/bitcoin',
        gitref=None,  # don't build TODO remove
    )

    build_config = config.BenchBuild()
    ibd_config = config.BenchIbdFromLocal(end_height=end_height)
    compiler = config.Compilers.gcc

    cfg = config.Config(
        to_bench=targets,
        workdir=workdir,
        synced_peer=peer,
        compilers=[compiler],
        safety_checks=False,
    )

    _startup_assertions(cfg)
    atexit.register(_get_shutdown_handler(cfg))

    results: t.List[benchmarks.Benchmark] = []

    for i, ts in enumerate([targets] * 2):
        for target in ts:
            git.checkout_in_dir(workdir / 'bitcoin', target)

            build = benchmarks.Build(cfg, build_config, compiler, target, i)
            build.run(cfg, build_config)
            assert build.gitco
            results.append(build)

            ibd = benchmarks.IbdLocal(cfg, ibd_config, compiler, target, i)
            ibd.run(cfg, ibd_config)
            assert ibd.gitco
            results.append(ibd)

            # Crucial that we do this else we muck up the cache.
            sh.rm(workdir / 'bitcoin')

    _persist_results(cfg, results)
    _print_results(cfg, results)


@cli.cmd
def run(yaml_filename: Path):
    """Do a benchmark run based on a yaml configuration file."""
    config_file = Path(yaml_filename)
    if not config_file.exists():
        print(".yaml config file required as only argument",
              file=sys.stderr)
        sys.exit(1)

    cfg = config.load(config_file)
    assert cfg.workdir
    logging.configure_logger(
        cfg.workdir, 'DEBUG' if cli.args.verbose else 'INFO')

    if cfg.codespeed:
        results.Reporters.codespeed = results.CodespeedReporter(
            cfg.codespeed)

    G.slack = slack.Client(cfg.slack.webhook_url if cfg.slack else '')
    slack.attach_slack_handler_to_logger(cfg, G.slack, logger)

    atexit.register(_get_shutdown_handler(cfg))

    logger.info("Started on host %s (codespeed env %s)",
                config.HOSTNAME,
                cfg.codespeed.envname if cfg.codespeed else '[none]')
    logger.info(cfg.to_string(pretty=True))

    try:
        run_full_suite(cfg)
    except Exception:
        G.slack.send_to_slack_attachment(
            G.gitco, "Error", {},
            text=traceback.format_exc(), success=False)
        raise

    _persist_results(cfg, results.ALL_RUNS)
    _print_results()


def _persist_results(cfg, results):
    logger.info("Getting hardware information")
    hw = hwinfo.get_hwinfo(cfg.workdir, None)

    res_dict = {
        'runs': results,
        'hwinfo': hw,
    }

    try:
        results_path = cfg.results_dir / 'results.pickle'
        results_path.write_bytes(pickle.dumps(res_dict))
        logger.info(
            "Wrote serialized benchmark results to %s", results_path)
    except Exception:
        logger.exception("failed to pickle results")


@cli.cmd
def render(pickle_filename: Path):
    """Render (or re-render) the pickled results of a benchmark run."""
    unpickled = pickle.loads(Path(pickle_filename).read_bytes())
    results.ALL_RUNS = unpickled['runs']
    results.HWINFO = unpickled['hwinfo']

    _print_results()


@cli.cmd
def setup():
    # user = sh.run('whoami').stdout.strip()
    # print(dedent("""
    #     In another terminal, add to /etc/sudoers:

    # """))
    pass


def _print_results(cfg: config.Config = None,
                   all_runs: t.List[benchmarks.Benchmark] = None) -> None:
    all_runs = all_runs or results.ALL_RUNS
    grouped = output.GroupedRuns.from_list(all_runs)

    if not cfg:
        cfg = list(list(grouped.values())[0].values())[0][0].cfg

    if len(cfg.to_bench) <= 1:
        timestr = output.get_times_table(grouped)
        print(timestr)
    else:
        output.print_comparative_times_table(cfg, grouped)
        output.make_plots(cfg, grouped)


def main():
    try:
        cli.run()
    except Exception:
        # Release lockfile if we've got it
        if G.lockfile_held:
            LOCKFILE_PATH.unlink()
            G.lockfile_held = False
            logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

        raise


if __name__ == '__main__':
    main()
