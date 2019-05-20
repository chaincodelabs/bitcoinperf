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

from . import bitcoind, results, sh, config
from .globals import G
from .logging import get_logger
from .sh import popen
from .results import HeightData

logger = get_logger()


class Benchmark(abc.ABC):
    name: str = ""
    cfg_class: t.Type[config.Bench]
    results_class: t.Type[results.Results] = results.Results

    def __init__(self,
                 cfg: dict,
                 target: config.Target,
                 run_idx: int = 0):
        self.cfg = cfg
        self.run_idx = run_idx
        self.bench_cfg = getattr(cfg.benches, self.name, {})
        self.compiler = copy.copy(G.compiler)
        self.gitco = copy.copy(G.gitco)
        self.target = target
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

    def _teardown(self, cfg, bench_cfg):
        """Any teardown that should always happen after the benchmark."""
        pass

    def wrapped_run(self, cfg, bench_cfg):
        """Called externally."""
        if not (cfg.no_caution or cfg.no_cache_drop):
            sh.drop_caches()

        G.benchmark = self.__class__

        logger.info("[%s] starting", self.id)
        try:
            self._run(cfg, bench_cfg)
        except Exception:
            logger.info("[%s] failed with an exception", self.id)
            raise
        finally:
            self._teardown()

        logger.info("[%s] done", self.id)

    def _try_execute_and_report(
            self, cmd, *, num_tries=1, executable='bitcoind'):
        """
        Attempt to execute some command a number of times and then report
        its execution memory usage or execution time to codespeed over HTTP.
        """
        for i in range(num_tries):
            cmd = sh.Command(cmd, self.name)
            cmd.start()
            cmd.join()

            if not cmd.check_for_failure():
                # Command succeeded
                _log_bench_result(True, self.id, cmd)
                self.results.total_time = cmd.total_secs
                self.results.peak_rss_kb = cmd.memusage_kib()
                results.save_result(
                    G.gitco, self.name, cmd.total_secs, cmd.memusage_kib(),
                    executable)
                return True

        _log_bench_result(False, self.id, cmd)
        return False


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

        self._try_execute_and_report(
            self.id,
            "make -j %s" % cfg.make_jobs,
            executable='make')

        if cfg.use_build_cache:
            cache = self._get_cache_path()
            logger.info("Copying build to cache %s", cache)
            shutil.copytree(cfg.workdir / 'bitcoin', cache)

    def _get_cache_path(self):
        return (
            self.cfg.build_cache_path() /
            "{}-{}".format(
                self.cfg.current_git_co.sha, self.cfg.current_compiler))

    def _restore_from_cache(self, cfg) -> True:
        cache = self._get_cache_path()
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


class MakeCheck(Benchmark):
    name = 'makecheck'
    id_format = 'makecheck.{G.compiler}.j={bench_cfg.num_jobs}'

    @dataclass
    class Results:
        total_time: int = None

    def _run(self, cfg, bench_cfg):
        self._try_execute_and_report(
            self.id,
            "make -j %s check" % (cfg.nproc - 1),
            num_tries=3, executable='make')


class FunctionalTests(Benchmark):
    name = 'functionaltests'
    id_format = 'functionaltests.{G.compiler}.j={bench_cfg.num_jobs}'

    @dataclass
    class Results:
        total_time: int = None

    def _run(self, cfg, bench_cfg):
        self._try_execute_and_report(
            self.id,
            "./test/functional/test_runner.py",
            num_tries=3, executable='functional-test-runner')


class Microbench(Benchmark):
    name = 'microbench'
    id_format = 'micro.{G.compiler}.j={bench_cfg.num_jobs}'
    results_class = results.MicrobenchResults

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
            self.results.bench_to_time[bench] = median
            results.save_result(
                G.gitco,
                'micro.{G.compiler}.{bench}'.format(G=G, bench=bench),
                total_secs=median,
                memusage_kib=None,
                executable='bench-bitcoin',
                extra_data={'result_max': max_, 'result_min': min_})


class IbdBench(abc.ABC, Benchmark):
    name = 'ibd'
    results_class = results.IbdResults

    def _get_server_node(self, cfg, bench_cfg) -> bitcoind.Node:
        # This might return None if we're IBDing from network.
        self.server_node = bitcoind.get_synced_node(cfg)
        return self.server_node

    def _get_client_node(self, cfg, bench_cfg) -> bitcoind.Node:
        pass

    def _get_codespeed_bench_name(self, current_height) -> str:
        if self.bench_cfg.start_height:
            fmt = "{self.name}.{start_height}.{current_height}.dbcache={dbcache}"  # nopep8
        else:
            fmt = "{self.name}.{current_height}.dbcache={dbcache}"

        return fmt.format(
            self=self,
            current_height=current_height,
            start_height=self.bench_cfg.start_height,
            dbcache=self.client_node.get_args_dict()['dbcache'])

    def _run(self, cfg, bench_cfg):
        self._ibd_setup()
        self.server_node = self._get_server_node()
        self.client_node = client_node = self._get_client_node()

        starting_height = client_node.wait_for_init()
        last_height_seen = starting_height
        last_resource_usage = None
        start_time = None

        extra_data = {
            'start_height': bench_cfg.start_height,
            **client_node.get_args_dict()
        }

        report_to_codespeed_heights: t.List[int] = list(bench_cfg.time_heights)

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
            time_now = time.time() - start_time
            last_resource_usage = client_node.cmd.get_resource_usage()

            # Codespeed
            # -----------------------------------------------------------------
            #
            # Consume any heights which have been passed but not yet reported
            # on.
            while report_to_codespeed_heights and \
                    report_to_codespeed_heights[0] <= last_height_seen:
                report_at_height = report_to_codespeed_heights.pop(0)
                results.save_result(
                    self.gitco,
                    self._get_codespeed_bench_name(last_height_seen),
                    time_now,
                    client_node.cmd.memusage_kib(),
                    'bitcoind',
                    extra_data={'height': last_height_seen, **extra_data},
                )

            # Results kept in-memory for later processing
            # -----------------------------------------------------------------
            self.results.height_to_data[last_height_seen] = HeightData(
                time_now,
                last_resource_usage.rss_kb,
                last_resource_usage.cpu_percent,
                last_resource_usage.num_fds,
            )

            time.sleep(1)

        final_time = time.time() - start_time
        final_name = self._get_codespeed_bench_name(last_height_seen)

        # Don't finalize results if the IBD was a failure.
        #
        if not _check_for_ibd_failure(cfg, client_node):
            logger.info("IBD failed")
            _log_bench_result(
                False, final_name, self.client_node.cmd)
            return False

        _log_bench_result(
            True, final_name, self.client_node.cmd)

        # Mark measurements for all heights remaining.
        #
        while report_to_codespeed_heights and \
                report_to_codespeed_heights[0] <= last_height_seen:
            report_at_height = report_to_codespeed_heights.pop(0)
            results.save_result(
                self.gitco,
                self._get_codespeed_bench_name(report_at_height),
                time_now,
                client_node.cmd.memusage_kib(),
                'bitcoind',
                extra_data={'height': last_height_seen, **extra_data},
            )

        # Record the time-to-tip if we didn't specify an end height.
        #
        if not bench_cfg.end_height:
            results.save_result(
                self.gitco,
                self._get_codespeed_bench_name('tip'),
                final_time,
                client_node.cmd.memusage_kib(),
                'bitcoind',
                extra_data={'height': last_height_seen, **extra_data},
            )

        self.results.total_time = final_time
        self.results.peak_rss_kb = self.client_node.cmd.memusage_kib()

        if last_resource_usage:
            self.results.height_to_data[last_height_seen] = HeightData(
                final_time,
                last_resource_usage.rss_kb,
                last_resource_usage.cpu_percent,
                last_resource_usage.num_fds,
            )

    def _teardown(self):
        """
        Shut down all the nodes we started and stash the datadir if need be.
        """
        self.client_node.stop_via_rpc()
        if self.server_node:
            self.server_node.stop_via_rpc()
        self.client_node.join()
        if self.server_node:
            self.server_node.join()

        if self.bench_cfg.stash_datadir:
            shutil.move(G.workdir / 'data', self.bench_cfg.stash_datadir)
            logger.info("Stashed datadir from %s -> %s",
                        G.workdir / 'data',
                        self.bench_cfg.stash_datadir)


class IbdLocal(IbdBench):
    name = 'ibd.local'

    def _get_client_node(self, cfg, bench_cfg):
        self.client_node = bitcoind.Node(
            G.workdir / 'bitcoin' / 'src' / 'bitcoind',
            G.workdir / 'data',
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.empty_datadir()

        client_start_kwargs = {
            'listen': 0,
            'connect': 1 if cfg.ibd_from_network else 0,
            'addnode': '' if cfg.ibd_from_network else cfg.ibd_peer_address,
        }

        if self.server_node:
            client_start_kwargs['addnode'] = '127.0.0.1:{}'.format(
                self.server_node.port)
        else:
            client_start_kwargs['addnode'] = cfg.synced_peer.address

        self.client_node.start(**client_start_kwargs)
        return self.client_node


class IbdRangeLocal(IbdBench):
    name = 'ibd.local'  # Range is reflected in starting height

    def _get_client_node(self, cfg, bench_cfg):
        self.client_node = bitcoind.Node(
            G.workdir / 'bitcoin' / 'src' / 'bitcoind',
            G.workdir / 'data',
            copy_from_datadir=bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        # Don't empty datadir since we just copied it from a pruned source.

        client_start_kwargs = {
            'listen': 0,
            'connect': 0,
            'addnode': cfg.ibd_peer_address,
        }

        if self.server_node:
            client_start_kwargs['addnode'] = '127.0.0.1:{}'.format(
                self.server_node.port)
        else:
            client_start_kwargs['addnode'] = cfg.synced_peer.address

        self.client_node.start(**client_start_kwargs)
        return self.client_node


class IbdReal(IbdBench):
    name = 'ibd.real'

    def _get_client_node(self, cfg, bench_cfg):
        self.client_node = bitcoind.Node(
            G.workdir / 'bitcoin' / 'src' / 'bitcoind',
            G.workdir / 'data',
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.empty_datadir()
        self.client_node.start()
        return self.client_node


class Reindex(IbdBench):
    name = 'reindex'

    def _get_server_node(self, cfg, bench_cfg):
        return None

    def _get_client_node(self, cfg, bench_cfg):
        self.client_node = bitcoind.Node(
            G.workdir / 'bitcoin' / 'src' / 'bitcoind',
            bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.start(**{
            'reindex': 1,
        })
        return self.client_node


class ReindexChainstate(IbdBench):
    name = 'reindex_chainstate'

    def _get_server_node(self, cfg, bench_cfg):
        return None

    def _get_client_node(self, cfg, bench_cfg):
        self.client_node = bitcoind.Node(
            G.workdir / 'bitcoin' / 'src' / 'bitcoind',
            bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.start(**{
            'reindex_chainstate': 1,
        })
        return self.client_node


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
