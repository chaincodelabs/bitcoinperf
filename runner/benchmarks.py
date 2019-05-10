import abc
import contextlib
import os
import time
import datetime
import shutil
import typing as t
from pathlib import Path

from marshmallow import Schema, fields

from . import bitcoind, results, sh
from .logging import get_logger
from .sh import popen
from .globals import G_

logger = get_logger()


class Names:
    IBD_REAL         = 'ibd.real.{height}.dbcache={dbcache}'
    IBD_LOCAL        = 'ibd.local.{height}.dbcache={dbcache}'
    IBD_LOCAL_RANGE  = 'ibd.local.{start_height}.{height}.dbcache={dbcache}'
    REINDEX          = 'reindex.{height}.dbcache={dbcache}'
    MICRO            = 'micro.{compiler}.{bench}'
    FUNC_TESTS       = 'functionaltests.{compiler}'
    MAKE_CHECK       = 'makecheck.{compiler}.{nproc}'
    MAKE             = 'build.make.{j}.{compiler}'


def benchmark(name):
    """A decorator used to declare benchmark steps.

    Handles skipping and count-based execution."""
    def wrapper(func):
        def inner(cfg, *args_, **kwargs):
            if name not in cfg.benches_to_run:
                logger.debug("Skipping benchmark %r", name)
            else:
                count = cfg.run_counts.get(name, 1)
                logger.info("Running benchmark %r %d times", name, count)
                # Drop system caches to ensure fair runs.
                if not cfg.no_caution:
                    sh.drop_caches()

                for _ in range(count):
                    func(cfg, *args_, **kwargs)

        return inner
    return wrapper


@contextlib.contextmanager
def timer(name: str):
    start = time.time()
    yield
    results.REF_TO_NAME_TO_TIME[G_.gitco.ref][name].append(
        time.time() - start)


class Benchmark(abc.ABC):
    name: str = ""
    # The bench-specific config, set at runtime
    bench_cfg: object = None
    should_always_run: bool = False

    def __init__(self, cfg: dict, run_idx: int = 0):
        self.cfg = cfg
        self.run_idx = run_idx
        self.bench_cfg = cfg['benches'].get(self.name, {})
        self.run_data = {}

    @classmethod
    def check_cfg(cls, cfg, bench_cfg) -> t.List[str]:
        """Run at startup. Returns a list of errors."""

    @abc.abstractmethod
    def _run(self, cfg, bench_cfg):
        pass

    @property
    @abc.abstractproperty
    def id(self) -> t.Optional[str]:
        """An identifier incorporating all the run parameters."""
        return None

    def wrapped_run(self, cfg, bench_cfg):
        """Called externally."""
        if not (cfg.no_caution or cfg.no_cache_drop):
            sh.drop_caches()

        logger.info("[%s] starting", self.id)
        self._run(cfg, bench_cfg)
        logger.info("[%s] done", self.id)


benchmarks: t.List[Benchmark] = []


class GitClone(PrepBenchmark):
    name = 'gitclone'
    should_always_run = True

    @property
    def id(self):
        return self.name

    def _run(self, cfg, bench_cfg):
        sh.run("git clone -b {} {} {}".format(
            cfg.repo_branch, cfg.repo_location, G_.workdir / 'bitcoin'))

        # For all subsequent benchmarks, sit in the bitcoin/ dir.
        os.chdir(G_.workdir / 'bitcoin')


class Build(PrepBenchmark):
    name = 'build'
    should_always_run = True

    @property
    def id(self):
        return self.name

    def _run(self, cfg, bench_cfg):
        cache = cfg.build_cache_path / G_.gitco.sha
        if cfg.use_build_cache and cache.exists():
            logger.info(
                "Cached version of build %s found - "
                "restoring from that and skipping build ", G_.gitco.sha)
            os.chdir(G_.workdir)
            if (G_.workdir / 'bitcoin').exists():
                sh.rm(G_.workdir / 'bitcoin')
            os.symlink(cache, G_.workdir / 'bitcoin')
            os.chdir(G_.workdir / 'bitcoin')

            return

        logger.info("Building db4")
        sh.run("./contrib/install_db4.sh .")

        my_env = os.environ.copy()
        my_env['BDB_PREFIX'] = "%s/bitcoin/db4" % G_.workdir

        sh.run("./autogen.sh")

        configure_prefix = ''
        if G_.compiler == 'clang':
            configure_prefix = 'CC=clang CXX=clang++ '

        # Ensure build is clean.
        makefile_path = G_.workdir / 'bitcoin' / 'Makefile'
        if makefile_path.is_file() and not cfg.no_clean:
            sh.run('make distclean')

        boostflags = ''
        armlib_path = '/usr/lib/arm-linux-gnueabihf/'

        if Path(armlib_path).is_dir():
            # On some architectures we need to manually specify this,
            # otherwise configuring with clang can fail.
            boostflags = '--with-boost-libdir=%s' % armlib_path

        logger.info("Running ./configure [...]")
        sh.run(
            configure_prefix +
            './configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" '
            'BDB_CFLAGS="-I${BDB_PREFIX}/include" '
            # Ensure ccache is disabled so that subsequent make runs
            # are timed accurately.
            '--disable-ccache ' + boostflags,
            env=my_env)

        _try_execute_and_report(
            Names.MAKE.format(
                j=cfg.make_jobs, compiler=G_.compiler),
            "make -j %s" % cfg.make_jobs,
            executable='make')

        if cfg.use_build_cache:
            logger.info("Copying build to cache %s", cache)
            shutil.copytree(G_.workdir / 'bitcoin', cache)


@benchmark('makecheck')
def bench_makecheck(cfg):
    _try_execute_and_report(
        Names.MAKE_CHECK.format(
            compiler=G_.compiler, nproc=(cfg.nproc - 1)),
        "make -j %s check" % (cfg.nproc - 1),
        num_tries=3, executable='make')


@benchmark('functionaltests')
def bench_functests(cfg):
    _try_execute_and_report(
        Names.FUNC_TESTS.format(compiler=G_.compiler),
        "./test/functional/test_runner.py",
        num_tries=3, executable='functional-test-runner')


@benchmark('microbench')
def bench_microbench(cfg):
    with timer("microbench.%s" % G_.compiler):
        if not cfg.no_caution:
            sh.drop_caches()
        microbench_ps = popen("./src/bench/bench_bitcoin")
        (microbench_stdout,
         microbench_stderr) = microbench_ps.communicate()

    if microbench_ps.returncode != 0:
        text = "stdout:\n%s\nstderr:\n%s" % (
            microbench_stdout.decode(), microbench_stderr.decode())

        cfg.slack_client.send_to_slack_attachment(
            G_.gitco, "Microbench exited with code %s" %
            microbench_ps.returncode, {}, text=text, success=False)

    microbench_lines = [
        # Skip the first line (header)
        i.decode().split(', ')
        for i in microbench_stdout.splitlines()[1:]]

    for line in microbench_lines:
        # Line strucure is
        # "Benchmark, evals, iterations, total, min, max, median"
        assert len(line) == 7
        (bench, median, max_, min_) = (
            line[0], float(line[-1]), float(line[-2]), float(line[-3]))
        if not max_ >= median >= min_:
            logger.warning(
                "%s has weird results: %s, %s, %s" %
                (bench, max_, median, min_))
            assert False
        results.save_result(
            G_.gitco,
            Names.MICRO.format(
                compiler=G_.compiler, bench=bench),
            total_secs=median,
            memusage_kib=None,
            executable='bench-bitcoin',
            extra_data={'result_max': max_, 'result_min': min_})


@benchmark('ibd')
def bench_ibd(cfg):
    bench_name_fmt = (
        Names.IBD_REAL if cfg.ibd_from_network else Names.IBD_LOCAL)

    if cfg.copy_from_datadir:
        bench_name_fmt = Names.IBD_LOCAL_RANGE

    checkpoints = list(
        cfg.ibd_checkpoints_as_ints + (['tip'] if cfg.ibd_to_tip else []))

    # This might return None if we're IBDing from network.
    server_node = bitcoind.get_synced_node(cfg)
    client_node = bitcoind.Node(
        G_.workdir / 'bitcoin' / 'src' / 'bitcoind',
        G_.workdir / 'data',
        copy_from_datadir=cfg.copy_from_datadir,
        extra_args=cfg.client_bitcoind_args,
    )

    if not cfg.copy_from_datadir:
        client_node.empty_datadir()

    client_start_kwargs = {
        'txindex': 0 if '-prune' in cfg.client_bitcoind_args else 1,
        'listen': 0,
        'connect': 1 if cfg.ibd_from_network else 0,
        'addnode': '' if cfg.ibd_from_network else cfg.ibd_peer_address,
        'dbcache': cfg.bitcoind_dbcache,
        'assumevalid': cfg.bitcoind_assumevalid,
    }

    if server_node:
        client_start_kwargs['addnode'] = '127.0.0.1:{}'.format(
            server_node.port)

    client_node.start(**client_start_kwargs)
    starting_height = client_node.wait_for_init()

    failure_count = 0
    last_height_seen = starting_height
    next_checkpoint = checkpoints.pop(0) if checkpoints else None

    def report_ibd_result(command, height):
        # Report to codespeed for this blockheight checkpoint
        results.save_result(
            G_.gitco,
            bench_name_fmt.format(
                start_height=starting_height,
                height=height,
                dbcache=cfg.bitcoind_dbcache),
            command.total_secs,
            command.memusage_kib(),
            executable='bitcoind',
            extra_data={
                'txindex': client_start_kwargs['txindex'],
                'start_height': starting_height,
                'height': height,
                'dbcache': cfg.bitcoind_dbcache,
            },
        )

    # Poll the running bitcoind process for its current height and report
    # results whenever we've crossed one of the user-specific checkpoints.
    #
    while True:
        info = client_node.call_rpc("getblockchaininfo")

        if not info:
            failure_count += 1
            if failure_count > 20:
                logger.error(
                    "Bitcoind hasn't responded to RPC in a suspiciously "
                    "long time... hung?")
                break
            time.sleep(1)
            continue

        last_height_seen = info['blocks']
        logger.debug("Saw height %s", last_height_seen)

        # If we have a next checkpoint, and it isn't just "sync to tip,"
        # and we've passed it, then record results now.
        #
        if next_checkpoint and \
                next_checkpoint != 'tip' and \
                last_height_seen >= next_checkpoint:
            # Report to codespeed for this blockheight checkpoint
            report_ibd_result(client_node.cmd, next_checkpoint)
            next_checkpoint = checkpoints.pop(0) if checkpoints else None

            if not next_checkpoint:
                logger.debug("Out of checkpoints - shutting down client")
                break

        if client_node.ps.returncode is not None or \
                info["verificationprogress"] > 0.9999:
            logger.debug("IBD complete or failed: %s", info)
            break

        time.sleep(1)

    client_node.stop_via_rpc()
    client_node.join()

    if not _check_for_ibd_failure(cfg, client_node):
        for height in checkpoints:
            report_ibd_result(client_node.cmd, height)

    server_node.stop_via_rpc()
    server_node.join()


@benchmark('reindex')
def bench_reindex(cfg):
    node = bitcoind.Node(
        G_.workdir / 'bitcoin' / 'src' / 'bitcoind',
        G_.workdir / 'data',
    )

    checkpoints = list(sorted(
        int(i) for i in cfg.ibd_checkpoints.replace("_", "").split(",")))
    checkpoints = checkpoints or ['tip']

    bench_name = Names.REINDEX.format(
        height=checkpoints[-1], dbcache=cfg.bitcoind_dbcache)
    node.start(reindex=1)
    height = node.wait_for_init()
    node.ps.wait()

    if not _check_for_ibd_failure(cfg, node):
        results.save_result(
            G_.gitco,
            bench_name,
            node.cmd.total_secs,
            node.cmd.memusage_kib(),
            executable='bitcoind',
            extra_data={
                'height': height,
                'dbcache': cfg.bitcoind_dbcache,
            },
        )


def _try_execute_and_report(
        bench_name, cmd, *, num_tries=1, executable='bitcoind'):
    """
    Attempt to execute some command a number of times and then report
    its execution memory usage or execution time to codespeed over HTTP.
    """
    for i in range(num_tries):
        cmd = sh.Command(cmd, bench_name)
        cmd.start()
        cmd.join()

        if not cmd.check_for_failure():
            _log_bench_result(True, bench_name, cmd)
            # Command succeeded
            break

        if i == (num_tries - 1):
            return False

    results.save_result(
        G_.gitco, bench_name, cmd.total_secs, cmd.memusage_kib(), executable)
    return True


def _log_bench_result(succeeded: bool, bench_name: str, cmd: sh.Command):
    if not succeeded:
        logger.error(
            "[%s] command failed with code %d\nstdout:\n%s\nstderr:\n%s",
            bench_name,
            cmd.returncode,
            cmd.stdout.decode()[-10000:],
            cmd.stderr.decode()[-10000:])
    else:
        logger.info(
            "[%s] command finished successfully in %.3f seconds (%s) "
            "with maximum resident set size %.3f MiB",
            bench_name, cmd.total_secs,
            datetime.timedelta(seconds=cmd.total_secs),
            cmd.memusage_kib() / 1024)


def _check_for_ibd_failure(cfg, node):
    failed = node.ps.returncode != 0 or node.check_disk_low()
    _log_bench_result(not failed, 'ibd or reindex', node.cmd)
    return failed
