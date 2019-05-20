import abc
import copy
import os
import time
import datetime
import shutil
import glob
import typing as t
from pathlib import Path
from dataclasses import dataclass, field

from . import bitcoind, results, sh, config, results
from .globals import G
from .logging import get_logger
from .sh import popen

logger = get_logger()


class Names:
    IBD_REAL         = 'ibd.real.{height}.dbcache={dbcache}'
    IBD_LOCAL        = 'ibd.local.{height}.dbcache={dbcache}'
    IBD_LOCAL_RANGE  = 'ibd.local.{start_height}.{height}.dbcache={dbcache}'
    REINDEX          = 'reindex.{height}.dbcache={dbcache}'


class Benchmark(abc.ABC):
    name: str = ""
    cfg_class: t.Type[config.Bench]
    results_class: t.Type[results.Results]

    def __init__(self, cfg: dict, run_idx: int = 0):
        self.cfg = cfg
        self.run_idx = run_idx
        self.bench_cfg = getattr(cfg.benches, self.name, {})
        self.compiler = copy.copy(G.compiler)
        self.gitco = copy.copy(G.gitco)
        self.id: str = self.id_format.format(
            cfg=cfg, G=G, bench_cfg=self.bench_cfg)

        # Each subclass must define a Results class, which defines the schema
        # of result data that the bench run will yield.
        self.results = self.results_class()

    @property
    @abc.abstractproperty
    def id_format(self) -> str:
        """An identifier incorporating all the run parameters."""
        return ""

    @abc.abstractmethod
    def _run(self, cfg, bench_cfg):
        """Run the actual benchmark."""
        pass

    def wrapped_run(self, cfg, bench_cfg):
        """Called externally."""
        if not (cfg.no_caution or cfg.no_cache_drop):
            sh.drop_caches()

        G.benchmark = self.__class__

        logger.info("[%s] starting", self.id)
        self._run(cfg, bench_cfg)
        logger.info("[%s] done", self.id)


benchmarks: t.List[Benchmark] = []


class Build(Benchmark):
    name = 'build'
    id_format = 'build.make.{cfg.num_build_jobs}.{G.compiler}'

    def _run(self, cfg, bench_cfg):
        self._clean_out_cache()
        if self._restore_from_cache():
            return

        logger.info("Building db4")
        sh.run("./contrib/install_db4.sh .")

        my_env = os.environ.copy()
        my_env['BDB_PREFIX'] = "%s/bitcoin/db4" % cfg.workdir

        sh.run("./autogen.sh")

        configure_prefix = ''
        if G.compiler == 'clang':
            configure_prefix = 'CC=clang CXX=clang++ '

        # Ensure build is clean.
        makefile_path = cfg.workdir / 'bitcoin' / 'Makefile'
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
                j=cfg.make_jobs, compiler=G.compiler),
            "make -j %s" % cfg.make_jobs,
            executable='make')

        if cfg.use_build_cache:
            cache = self.get_cache_path()
            logger.info("Copying build to cache %s", cache)
            shutil.copytree(cfg.workdir / 'bitcoin', cache)

    def get_cache_path(self):
        return (
            self.cfg.build_cache_path() /
            "{}-{}".format(
                self.cfg.current_git_co.sha, self.cfg.current_compiler))

    def _restore_from_cache(self, cfg) -> True:
        cache = self.get_cache_path()
        cache_bitcoind = cache / 'src' / 'bitcoind'
        cache_bitcoincli = cache / 'src' / 'bitcoin-cli'

        if cfg.use_build_cache and cache.exists():
            if not (cache_bitcoind.exists() and cache_bitcoincli.exists()):
                logger.warning(
                    "Incomplete cache found at %s; rebuilding", cache)
                sh.rm(cache)

            logger.info(
                "Cached version of build %s found - "
                "restoring from that and skipping build ", G.gitco.sha)
            os.chdir(cfg.workdir)
            if (cfg.workdir / 'bitcoin').exists():
                sh.rm(cfg.workdir / 'bitcoin')
            os.symlink(cache, cfg.workdir / 'bitcoin')
            os.chdir(cfg.workdir / 'bitcoin')

            return True
        return False

    def _clean_out_cache(self, cfg):
        cache = cfg.build_cache_path / G.gitco.sha
        files_in_cache = glob.glob("{}/*".format(cache))
        files_in_cache.sort(key=lambda x: os.path.getmtime(x))

        for stale in files_in_cache[cfg.cache_build_size:]:
            sh.rm(stale)


benchmarks.append(Build)


class MakeCheck(Benchmark):
    name = 'makecheck'
    id_format = 'makecheck.{G.compiler}.j={bench_cfg.num_jobs}'

    @dataclass
    class Results:
        total_time: int = None

    def _run(self, cfg, bench_cfg):
        _try_execute_and_report(
            self.id,
            "make -j %s check" % (cfg.nproc - 1),
            num_tries=3, executable='make')


benchmarks.append(MakeCheck)


class FunctionalTests(Benchmark):
    name = 'functionaltests'
    id_format = 'functionaltests.{G.compiler}.j={bench_cfg.num_jobs}'

    @dataclass
    class Results:
        total_time: int = None

    def _run(self, cfg, bench_cfg):
        _try_execute_and_report(
            self.id,
            "./test/functional/test_runner.py",
            num_tries=3, executable='functional-test-runner')


benchmarks.append(FunctionalTests)


class Microbench(Benchmark):
    name = 'microbench'
    id_format = 'micro.{G.compiler}.j={bench_cfg.num_jobs}'

    @dataclass
    class Results:
        total_time: int = None
        bench_to_time: t.Dict[str, float] = field(default_factory=dict)

    def _run(self, cfg, bench_cfg):
        time_start = time.time()
        if not cfg.no_caution:
            sh.drop_caches()
        microbench_ps = popen("./src/bench/bench_bitcoin")
        (microbench_stdout,
         microbench_stderr) = microbench_ps.communicate()
        self.results.total_time = (time.time() - time_start)

        if microbench_ps.returncode != 0:
            text = "stdout:\n%s\nstderr:\n%s" % (
                microbench_stdout.decode(), microbench_stderr.decode())

            cfg.slack_client.send_to_slack_attachment(
                G.gitco, "Microbench exited with code %s" %
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
                G.gitco,
                'micro.{G.compiler}.{bench}'.format(G=G, bench=bench),
                total_secs=median,
                memusage_kib=None,
                executable='bench-bitcoin',
                extra_data={'result_max': max_, 'result_min': min_})


benchmarks.append(Microbench)


class IbdBench(Bench):
    name = 'ibd.real'

    @dataclass
    class Results:
        total_time: int = None
        height_to_time: t.Dict[int, float] = field(default_factory=dict)

    def _ibd_setup(self):
        pass
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

    def _get_server_node(self) -> bitcoind.Node:
        pass

    def _get_client_node(self) -> bitcoind.Node:
        pass

    def _get_codespeed_bench_name(self) -> str:
        return ""

    def _run(self, cfg, bench_cfg):
        self._ibd_setup()
        server_node = self._get_server_node()
        client_node = self._get_client_node()

        starting_height = client_node.wait_for_init()
        last_height_seen = starting_height
        start_time = None

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
            if client_node.ps.returncode is not None:
                logger.info("node process died: %s", client_node)
                break

            (last_height_seen, progress) = (
                client_node.poll_for_height_and_progress())

            if not (last_height_seen and progress):
                raise RuntimeError(
                    "RPC calls to {} failed".format(client_node))
            elif last_height_seen >= bench_cfg.end_height or \
                    progress > 0.9999:
                logger.info("ending IBD based on height (%s) or progress (%s)",
                            last_height_seen, progress)
                break
            elif last_height_seen < bench_cfg.start_height:
                logger.debug("height (%s) not yet at min height %s",
                            last_height_seen, bench_cfg.start_height)
                time.sleep(0.5)
                continue

            start_time = start_time or time.time()
            report_ibd_result(client_node.cmd, next_checkpoint)

            time.sleep(1)

        final_time = time.time() - start_time

        if Reporters.codespeed and \
                not _check_for_ibd_failure(cfg, client_node):
            Reporters.codespeed.save_result(
                self.gitco, self._get_codespeed_bench_name(),
                final_time, executable='bitcoind',
            )

        client_node.stop_via_rpc()
        server_node.stop_via_rpc()
        client_node.join()
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
