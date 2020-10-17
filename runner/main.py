#!/usr/bin/env python3.8
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
import random
import time
import typing as t
from pathlib import Path
from textwrap import dedent

import clii

from . import (
    output, config, bitcoind, results, slack, benchmarks, logging, git, sh,
    hwinfo, util)
from .globals import G
from .logging import get_logger

logger = get_logger()

assert sys.version_info >= (3, 8), "Python 3.8 required"

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
    sh.run(r'find %s/* -type d -mtime +3 -exec rm -rf {} \;' % config.workdir_path)
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
    repodir = cfg.workdir / 'bitcoin'
    git.get_repo(repodir)
    checkouts, bad_targets = git.resolve_targets(repodir, cfg.to_bench)

    if bad_targets:
        logger.warning("Couldn't resolve git targets: %s", bad_targets)
        return

    config.link_latest_run(cfg)

    for target in cfg.to_bench:
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
            sh.cd(cfg.workdir)
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


def _missing_pkgs() -> t.List[str]:
    errs = []
    if 'GNU time' not in sh.run('/usr/bin/time --version').stderr:
        errs.append('Need to install GNU time (sudo apt install time)')

    if not sh.run('which fio', quiet=True).ok:
        errs.append('Need to install fio (sudo apt install fio)')

    return errs


@cli.cmd
def setup():
    """
    Run a guided setup of the fixture data needed to benchmark.
    """
    from .thirdparty import color as c  # type: ignore

    catchphrase = random.choice([
        "let's be honest, it's basically your only option",
        "barely adequate but almost certainly better than guessing",
        "just slightly easier to configure than autotools",
        "WITH_LOCK(::cs_main, chainstate->IsLoveReal())",
        "get out, before the rats eat you!",
    ])

    print(fr"""
  _    _ _          _                     __
 | |__(_) |_ __ ___(_)_ _  _ __  ___ _ _ / _|
 | '_ \ |  _/ _/ _ \ | ' \| '_ \/ -_) '_|  _|
 |_.__/_|\__\__\___/_|_||_| .__/\___|_| |_|
                          |_|

  {c.yellow(catchphrase)}
    """)

    def ent():
        input('\npress [enter] to continue ')

    print(dedent("""
        Bitcoinperf requires the existence of some data and git repos;
        we're going to set those up now.
    """), end='')
    ent()

    def div():
        print('\n' + '-' * 80 + '\n')

    div()

    _15m_load = os.getloadavg()[-1] > 1.0
    if _15m_load > 1.0:
        print(c.yellow(c.bold(dedent(f"""
            Warning: I've noticed your load is highish (15m avg: {_15m_load}).

            Please note that benchmark results are very suspect when run on
            a computer used for regular activity. If you're doing other things
            with the computer, the load may vary while bitcoinperf runs,
            skewing results.
        """))))

        ent()

    if not config.config_path.exists():
        config.config_path.mkdir()
        print('Created config dir at {config.config_path}')

    print(dedent(f"""
        Bitcoinperf benchmarks often rely on one bitcoind process, a fixture
        peer, serving data to the bitcoind process being benchmarked. Since the
        fixture peer needs data to serve, we have to prepopulate a repository
        and datadir used to create the synced peer.

        The bitcoin repo and datadir for this peer will be in

            {config.peer_path}/bitcoin
            {config.peer_path}/datadir
    """))
    ent()

    if not config.peer_path.exists():
        config.peer_path.mkdir()

    def yn(prompt: str) -> bool:
        return input(prompt).lower() in ['y', '']

    if not config.peer_repo.exists():
        print(dedent(f"""
            The peer requires a bitcoin repo to exist at

                {config.peer_repo}
        """))

        if yn('Clone bitcoin.git from GitHub? [Y/n] '):
            url = 'https://github.com/bitcoin/bitcoin.git'
            print(f'Cloning from {url}... ', end='')
            sys.stdout.flush()
            sh.run(f'git clone --depth 1 {url} {config.peer_repo}')
            print(c.green('finished!'))
            sys.stdout.flush()
            time.sleep(0.8)
        else:
            print(c.red(c.bold(dedent(f"""
                !! You'll need to provide a repo for the synced peer to
                   use at {config.peer_repo}, or use the networked peer option
                   (see config:SyncedPeer.address).

                   You can symlink a repo, if that floats your boat.
            """))))

    if not config.peer_datadir.exists():
        print(c.red(c.bold(dedent(f"""
            !! You'll also need to provide the synced peer with a
               populated datadir at

                 {config.peer_datadir}

               Either symlink or copy a datadir here that is synced to a height
               above the range you want to benchmark (probably above at least
               550,000).
        """))))

        print(c.cyan(c.bold(dedent("""
            !! Alternatively you can specify a network address to use in lieu
               of a local peer with the `--peer-address` flag. Bitcoinperf
               will (obviously) not manage the setup/teardown of this peer.
        """))))

        ent()

    if not config.base_datadirs.exists():
        config.base_datadirs.mkdir()

    if not config.pruned_500k_datadir.exists():
        print(dedent('''
            To do meaningful benchmarking, we often have to look at a region
            of the chain that is well past the first few hundred thousand
            blocks, since these blocks are not characteristic of where the
            IBD process bottlenecks.

            To this end, you can download a datadir that is pre-synced up to
            height 500k. We will seed the benchmark node from this datadir so
            that you can immediately start the benchmark from a part of the
            chain that is meaningful to examine for overall performance.
        '''))

        prompt = 'Download pre-synced, pruned 500k block datadir? [Y/n] '
        if yn(prompt):
            url = 'https://storage.googleapis.com/chaincode-bitcoinperf/pruned_500k.tar.gz'  # noqa
            print(f'Downloading and decompressing {url}...')
            print('└─ this will take about 15 minutes')
            sh.run(f'cd {config.base_datadirs} && curl {url} | tar xvz')
            sh.run(
                f'cd {config.base_datadirs} && '
                f'mv data/bitcoin_pruned_500k {config.pruned_500k_datadir} &&'
                f'rmdir data'
            )
            print(c.green(
                f'Datadir pruned to 500k stored at '
                f'{config.pruned_500k_datadir}'))
        else:
            print(c.red(c.bold(dedent(f"""
                Be warned: `bitcoinperf bench-pr` will not work out of the box
                without a datadir synced to 500k at

                    {config.pruned_500k_datadir}
                """))))

    print(c.blue(dedent("""
        Ensure you've installed all dependencies to compile bitcoin core
        locally. See

          - `./bin/install.sh` or
          - https://github.com/bitcoin/bitcoin/blob/master/doc/build-unix.md
    """)))

    pkgs = _missing_pkgs()

    if pkgs:
        print(c.red('Missing packages: '))
        for pkg_msg in pkgs:
            print(c.red(f'  - {pkg_msg}'))

    ent()

    username = getpass.getuser()
    print(c.blue(dedent(f"""
        Be sure you've added the following lines to your /etc/sudoers file
        so that we can drop caches:

         {username}     ALL = NOPASSWD: /sbin/sysctl vm.drop_caches=3
         {username}     ALL = NOPASSWD: /sbin/swapoff -a
    """)))

    print(c.green(dedent("""
        cool, have fun.

        `bitcoinperf bench-pr $PR_NUMBER` is probably what you want.
    """)))


@cli.cmd
def bench_pr(pr_num: str,
             run_id: str = None,
             peer_tag: str = 'v0.20.0rc2',
             peer_address: str = None,
             num_blocks: int = 1_000,
             run_count: int = 2,
             run_micros: bool = False,
             compare_ref: str = '',
             bitcoind_args: str = '',
             ):
    """
    Benchmark a PR relative to its merge base for some number of blocks,
    starting from height 500_000.

    Args:
        run_id: label for the run - will create /tmp/bitcoinperf-[run_id]
        peer_tag: which git tag the server bitcoind process will run
        peer_address: network address to use as peer instead of local instance
        num_blocks: the number of blocks to benchmark
        run_count: number of times to test IBD of each git ref
        run_micros: if true, run the microbenchmarks
        compare_ref: compare the PR against this git ref instead of inferred mergebase
        bitcoind_args: additional arguments to pass to bitcoind invocations
    """
    run_id = run_id or pr_num
    workdir = Path(f'/tmp/bitcoinperf-{run_id}')

    if workdir.exists():
        logger.warning(f'Removing existing (old?) workdir {workdir}')
        sh.rm(workdir)

    workdir.mkdir()
    logging.configure_logger(workdir, 'DEBUG' if cli.args.verbose else 'INFO')
    repodir = workdir / 'bitcoin'

    targets = [
        config.Target(
            name=f"#{pr_num}", gitref=f'pr/{pr_num}', rebase=False,
            bitcoind_extra_args=bitcoind_args),
    ]

    if compare_ref:
        if '/' not in compare_ref:
            compare_ref = f'origin/{compare_ref}'
        remote, ref = compare_ref.split('/')

        name = ref[:8] if util.is_hex(ref) else ref[:24]
        targets.append(config.Target(
            name=name, gitref=ref, gitremote=remote, rebase=False,
            bitcoind_extra_args=bitcoind_args))
    else:
        targets.append(config.Target(
            name=git.MERGEBASE_REF, gitref='master', gitremote='origin',
            rebase=False,
            bitcoind_extra_args=bitcoind_args))

    git.get_repo(repodir)
    checkouts, bad_targets = git.resolve_targets(repodir, targets)
    if bad_targets:
        print(f"failed to find commit for {[t.gitref for t in bad_targets]}")
        sys.exit(1)

    # This is hardcoded per the preexisting datadir.
    start_height = 500_000
    end_height = start_height + num_blocks

    peer_args: t.Dict[str, t.Union[str, Path]] = {}

    if peer_address:
        peer_args['address'] = peer_address
    else:
        peer_args.update(dict(
            datadir=config.peer_datadir,
            repodir=config.peer_repo,
            gitref=peer_tag,
        ))

    logger.info("Running benchmarks for:")
    for target in targets:
        logger.info("  %s", target.gitco)

    peer = config.SyncedPeer(**peer_args)

    build_config = config.BenchBuild()
    ibd_config = config.BenchIbdRangeFromLocal(
        src_datadir=config.pruned_500k_datadir,
        start_height=start_height,
        end_height=end_height,
    )

    cfg = config.Config(
        to_bench=targets,
        workdir=workdir,
        synced_peer=peer,
        compilers=[config.Compilers.gcc],
        safety_checks=False,
    )

    _startup_assertions(cfg)
    atexit.register(_get_shutdown_handler(cfg))

    results: t.List[benchmarks.Benchmark] = []

    if run_micros:
        for i, ts in enumerate([targets]):
            for target in ts:
                for compiler in config.Compilers:
                    git.checkout_in_dir(workdir / 'bitcoin', target)

                    build = benchmarks.Build(
                        cfg, build_config, compiler, target, i)
                    build.run(cfg, build_config)
                    assert build.gitco
                    results.append(build)

                    micro_conf = config.BenchMicrobench()
                    micro = benchmarks.Microbench(
                        cfg, micro_conf, compiler, target, i)
                    micro.run(cfg, micro_conf)
                    assert micro.gitco
                    results.append(micro)

    # Only do IBD benches with gcc since they're long and we ship binaries
    # built with gcc.
    compiler = config.Compilers.gcc

    for i, ts in enumerate([targets] * run_count):
        for target in ts:
            git.checkout_in_dir(workdir / 'bitcoin', target)

            build = benchmarks.Build(cfg, build_config, compiler, target, i)
            build.run(cfg, build_config)
            assert build.gitco

            ibd = benchmarks.IbdRangeLocal(
                cfg, ibd_config, compiler, target, i)
            ibd.run(cfg, ibd_config)
            assert ibd.gitco
            results.append(ibd)

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
        output.print_comparative_times_table(grouped, config=cfg)
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
