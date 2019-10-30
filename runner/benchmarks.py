import abc
import copy
import os
import time
import datetime
import shutil
import glob
import typing as t
from pathlib import Path

from . import bitcoind, results, sh, config, git
from .globals import G
from .logging import get_logger
from .sh import popen
from .results import HeightData

logger = get_logger()


class Benchmark(abc.ABC):
    name: str = ""
    _results_class: t.Type[results.Results] = results.Results

    def __init__(self,
                 cfg: config.Config,
                 bench_cfg: config.Bench,
                 target: config.Target,
                 run_idx: int = 0):
        self.cfg = cfg
        self.run_idx = run_idx
        self.bench_cfg = bench_cfg
        self.compiler = copy.copy(G.compiler)
        self.gitco = copy.copy(G.gitco)
        self.target = target
        self.id: str = self.id_format.format(
            cfg=cfg, G=G, bench_cfg=self.bench_cfg)

        # Each subclass must define a Results class, which defines the schema
        # of result data that the bench run will yield.
        self.results = self._results_class()

    def __getstate__(self):
        state = self.__dict__.copy()
        for attr in ('client_node', 'server_node'):
            if attr in state:
                del state[attr]

        return state

    @property
    @abc.abstractproperty
    def id_format(self) -> str:
        """An identifier incorporating all the run parameters."""
        return ""

    @abc.abstractmethod
    def _run(self, cfg, bench_cfg):
        """Run the actual benchmark."""
        pass

    def _teardown(self):
        """Any teardown that should always happen after the benchmark."""
        pass

    def wrapped_run(self, cfg, bench_cfg):
        """Called externally."""
        sh.drop_caches()

        G.benchmark = self.__class__

        logger.info("[%s] starting", self.id or self.name)
        try:
            self._run(cfg, bench_cfg)
        except Exception:
            logger.exception(
                "[%s] failed with an exception", self.id or self.name)
            raise
        finally:
            # FWIW, on Ctrl+C teardown is unconditionally handled by
            # `main._get_shutdown_handler`. Running this again shouldn't
            # cause a problem though.
            self._teardown()

        logger.info("[%s] done", self.id or self.name)

    def _try_execute_and_report(
            self, cmd_str, *, num_tries=1, executable='bitcoind'):
        """
        Attempt to execute some command a number of times and then report
        its execution memory usage or execution time to codespeed over HTTP.
        """
        for i in range(num_tries):
            cmd = sh.Command(cmd_str, self.name)
            cmd.start()
            cmd.join()

            if not cmd.check_for_failure():
                # Command succeeded
                _log_bench_result(True, self.id, cmd)
                self.results.total_time = cmd.total_secs
                self.results.peak_rss_kb = cmd.memusage_kib()
                results.report_result(self, self.id, cmd.total_secs)
                results.report_result(
                    self, self.id + '.mem-usage', cmd.memusage_kib())
                return True

        _log_bench_result(False, self.id, cmd)
        return False


class Build(Benchmark):
    name = 'build'
    id_format = 'build.make.{bench_cfg.num_jobs}.{G.compiler}'

    def _run(self, cfg, bench_cfg):
        # Important that we set this envvar before potentially early-exiting
        # from cache.
        os.environ['BDB_PREFIX'] = "%s/bitcoin/db4" % cfg.workdir

        self._clean_out_cache()
        if self._restore_from_cache():
            return

        logger.info("Building db4")
        sh.run("./contrib/install_db4.sh .")
        sh.run("./autogen.sh")

        configure_prefix = ''
        if G.compiler == 'clang':
            configure_prefix = 'CC=clang CXX=clang++ '

        # Ensure build is clean.
        makefile_path = cfg.workdir / 'bitcoin' / 'Makefile'
        if makefile_path.is_file() and cfg.clean:
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
            + ' {} '.format(bench_cfg.configure_args) +
            # Ensure ccache is disabled so that subsequent make runs
            # are timed accurately.
            '--disable-ccache ' + boostflags)

        self._try_execute_and_report(
            "make -j %s" % bench_cfg.num_jobs, executable='make')

        if cfg.cache_build:
            cache = self._get_cache_path()
            logger.info("Copying build to cache %s", cache)
            shutil.copytree(cfg.workdir / 'bitcoin', cache)

    def _get_cache_path(self):
        return (
            self.cfg.build_cache_path() /
            "{}-{}".format(self.gitco.sha, self.compiler))

    def _restore_from_cache(self) -> True:
        cache = self._get_cache_path()
        cache_bitcoind = cache / 'src' / 'bitcoind'
        cache_bitcoincli = cache / 'src' / 'bitcoin-cli'

        if self.cfg.cache_build and cache.exists():
            if not (cache_bitcoind.exists() and cache_bitcoincli.exists()):
                logger.warning(
                    "Incomplete cache found at %s; rebuilding", cache)
                sh.rm(cache)
                return False

            logger.info(
                "Cached version of build %s found - "
                "restoring from that and skipping build ", self.gitco.sha)
            os.chdir(self.cfg.workdir)
            if (self.cfg.workdir / 'bitcoin').exists():
                sh.rm(self.cfg.workdir / 'bitcoin')
            os.symlink(cache, self.cfg.workdir / 'bitcoin')
            os.chdir(self.cfg.workdir / 'bitcoin')

            msg = git.get_commit_msg('HEAD')
            if msg != self.gitco.commit_msg:
                raise RuntimeError(
                    "cache {} has bad HEAD (expected '{}', got '{}')!".format(
                        cache, self.gitco.commit_msg, msg))

            return True
        return False

    def _clean_out_cache(self):
        cache = self.cfg.build_cache_path()
        files_in_cache = glob.glob("{}/*".format(cache))
        files_in_cache.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        for stale in files_in_cache[self.cfg.cache_build_size:]:
            logger.info("Deleting stale cache %s", stale)
            sh.rm(Path(stale))


class MakeCheck(Benchmark):
    name = 'makecheck'
    id_format = 'makecheck.{G.compiler}.j={bench_cfg.num_jobs}'

    def _run(self, cfg, bench_cfg):
        self._try_execute_and_report(
            "make -j %s check" % (bench_cfg.num_jobs),
            num_tries=3, executable='make')


class FunctionalTests(Benchmark):
    name = 'functests'
    id_format = 'functionaltests.{G.compiler}.j={bench_cfg.num_jobs}'

    def _run(self, cfg, bench_cfg):
        self._try_execute_and_report(
            "./test/functional/test_runner.py",
            num_tries=3, executable='functional-test-runner')


class Microbench(Benchmark):
    name = 'microbench'
    id_format = 'micro.{G.compiler}.j={bench_cfg.num_jobs}'
    _results_class = results.MicrobenchResults

    def _run(self, cfg, bench_cfg):
        time_start = time.time()
        sh.drop_caches()
        cmd_str = "./src/bench/bench_bitcoin"

        if bench_cfg.filter:
            cmd_str += " -filter='{}'".format(bench_cfg.filter)

        # TODO: use sh.Command, report peak memory usage - maybe per bench?

        microbench_ps = popen(cmd_str)
        (microbench_stdout,
         microbench_stderr) = microbench_ps.communicate()
        self.results.total_time = (time.time() - time_start)

        if microbench_ps.returncode != 0:
            text = "stdout:\n%s\nstderr:\n%s" % (
                microbench_stdout.decode(), microbench_stderr.decode())

            G.slack.send_to_slack_attachment(
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
            results.report_result(
                self,
                'micro.{G.compiler}.{bench}'.format(G=G, bench=bench),
                median,
                extra_data={'result_max': max_, 'result_min': min_},
            )


class IbdBench(Benchmark):
    name = 'ibd'
    _results_class = results.IbdResults
    id_format = ''  # We use _get_codespeed_bench_name() instead.

    def __init__(self, *args, **kwargs):
        self.client_node = self.server_node = None
        super().__init__(*args, **kwargs)
        self.id = self._get_codespeed_bench_name(
            self.bench_cfg.end_height or 'tip')

    def _get_server_node(self) -> bitcoind.Node:
        # This might return None if we're IBDing from network.
        self.server_node = bitcoind.get_synced_node(self.cfg)
        return self.server_node

    def _get_client_node(self) -> bitcoind.Node:
        pass

    def _get_codespeed_bench_name(self, current_height) -> str:
        if self.bench_cfg.start_height:
            fmt = "{self.name}.{start_height}.{current_height}"
        else:
            fmt = "{self.name}.{current_height}"

        return fmt.format(
            self=self,
            current_height=current_height,
            start_height=self.bench_cfg.start_height)

    def _run(self, cfg, bench_cfg):
        self.server_node = self._get_server_node()
        self.client_node = client_node = self._get_client_node()

        starting_height = client_node.wait_for_init()
        last_height_seen = starting_height
        last_resource_usage = None
        start_time = None

        if self.server_node:
            server_blockchaininfo = self.server_node.call_rpc(
                'getblockchaininfo')
            client_blockchaininfo = self.client_node.call_rpc(
                'getblockchaininfo')
            server_blocks = server_blockchaininfo['blocks']
            client_headers = client_blockchaininfo['headers']

            if server_blocks < bench_cfg.end_height:
                raise RuntimeError(
                    ("Server blocks ({}) must be greater than end height "
                     "({}) otherwise the IBD will stall. "
                     "Sync the server's datadir to a height past {}."
                     ).format(server_blocks, bench_cfg.end_height,
                              bench_cfg.end_height))

            if server_blocks <= client_headers:
                raise RuntimeError(
                    ("Server blocks ({}) must be greater than client headers "
                     "({}) otherwise the IBD will stall since the server "
                     "cannot report a connected block better than the "
                     "client's existing header chain. "
                     "Sync the server's datadir to a height past {}."
                     ).format(server_blocks, client_headers, client_headers))

        extra_data = {
            'start_height': bench_cfg.start_height,
            **client_node.get_args_dict()
        }

        report_to_codespeed_heights: t.List[int] = list(
            bench_cfg.time_heights or [])
        iters = 0
        time_now = None

        # Poll the running bitcoind process for its current height and report
        # results whenever we've crossed one of the user-specific checkpoints.
        #
        while True:
            if client_node.ps.returncode is not None:
                logger.info("node process died: %s", client_node)
                break

            (last_height_seen, progress) = (
                client_node.poll_for_height_and_progress())

            logger.debug(
                "Last saw height=%s progress=%s", last_height_seen, progress)

            if last_height_seen is None and progress is None:
                raise RuntimeError(
                    "RPC calls to {} failed".format(client_node))

            elif (bench_cfg.end_height and
                  last_height_seen >= bench_cfg.end_height) or \
                    progress > 0.9999:
                # Be sure we've set start time in case the bench finished
                # really fast.
                start_time = start_time or time.time()

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
                results.report_result(
                    self,
                    self._get_codespeed_bench_name(report_at_height),
                    time_now,
                    extra_data={'height': last_height_seen, **extra_data},
                )
                results.report_result(
                    self,
                    self._get_codespeed_bench_name(report_at_height) +
                    '.mem-usage',
                    client_node.cmd.memusage_kib(),
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

            if iters % 120 == 0:
                logger.info(
                    "Last saw height=%s progress=%s",
                    last_height_seen, progress)
                logger.debug(
                    "Codespeed checkpoints left: %s",
                    report_to_codespeed_heights)

            iters += 1
            time.sleep(1)

        final_time = time.time() - start_time
        final_name = self._get_codespeed_bench_name(last_height_seen)

        before_shutdown = time.time()

        if client_node.ps.returncode is None:
            # Longer timeout - might be flushing cache
            client_node.stop_via_rpc(timeout=(60 * 25))
        else:
            client_node.ps.join()

        logger.info("Shutdown took %s seconds", time.time() - before_shutdown)

        # Don't finalize results if the IBD was a failure.
        #
        if _check_for_ibd_failure(client_node):
            logger.info("IBD failed")
            _log_bench_result(
                False, final_name, self.client_node.cmd)
            return False

        _log_bench_result(True, final_name, self.client_node.cmd)

        # Mark measurements for all heights remaining.
        #
        while report_to_codespeed_heights and \
                report_to_codespeed_heights[0] <= last_height_seen:
            report_at_height = report_to_codespeed_heights.pop(0)
            results.report_result(
                self,
                self._get_codespeed_bench_name(report_at_height),
                # time_now is None if command completed before a single
                # measurement.
                time_now or 0,
                extra_data={'height': last_height_seen, **extra_data},
            )
            results.report_result(
                self,
                self._get_codespeed_bench_name(report_at_height)
                + '.mem-usage',
                client_node.cmd.memusage_kib(),
                extra_data={'height': last_height_seen, **extra_data},
            )

        # Record the time-to-tip if we didn't specify an end height.
        #
        if progress > 0.999 and not bench_cfg.end_height:
            results.report_result(
                self,
                self._get_codespeed_bench_name('tip'),
                final_time,
                extra_data={'height': last_height_seen, **extra_data},
            )
            results.report_result(
                self,
                self._get_codespeed_bench_name('tip') + '.mem-usage',
                client_node.cmd.memusage_kib(),
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
        if self.client_node.is_process_alive:
            # Longer timeout - might be flushing cache
            self.client_node.stop_via_rpc(timeout=(60 * 25))
        if self.server_node:
            self.server_node.stop_via_rpc(timeout=120)

        if getattr(self.bench_cfg, 'stash_datadir', None):
            src_datadir = getattr(self.bench_cfg, 'src_datadir', None)

            # If the src_datadir is the same one that we'll stash to,
            # leave it in place.
            if src_datadir == self.bench_cfg.stash_datadir:
                return

            if self.bench_cfg.stash_datadir.exists():
                shutil.rmtree(self.bench_cfg.stash_datadir)

            (self.cfg.workdir / 'data').replace(self.bench_cfg.stash_datadir)
            logger.info("Stashed datadir from %s -> %s",
                        self.cfg.workdir / 'data',
                        self.bench_cfg.stash_datadir)
        else:
            datadir = self.cfg.workdir / 'data'
            if datadir.exists():
                shutil.rmtree(datadir)


class IbdLocal(IbdBench):
    name = 'ibd.local'

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / 'bitcoin' / 'src' / 'bitcoind',
            self.cfg.workdir / 'data',
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.empty_datadir()

        self.client_node.start(**{
            'listen': 0, 'connect': 0,
            'addnode': (
                '127.0.0.1:{}'.format(self.server_node.port) if
                self.server_node else self.cfg.synced_peer.address),
        })

        return self.client_node


class IbdRangeLocal(IbdBench):
    name = 'ibd.local.range'  # Range is reflected in starting height

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / 'bitcoin' / 'src' / 'bitcoind',
            self.cfg.workdir / 'data',
            copy_from_datadir=self.bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        # Don't empty datadir since we just copied it from a pruned source.

        self.client_node.start(**{
            'listen': 0,
            'connect': 0,
            'addnode': (
                '127.0.0.1:{}'.format(self.server_node.port) if
                self.server_node else self.cfg.synced_peer.address),
            # Set an unreasonably high prune target so that we can resume from
            # a pruned datadir but never actually prune.
            'prune': 9999999,
        })
        return self.client_node


class IbdReal(IbdBench):
    name = 'ibd.real'

    def _get_server_node(self):
        return None

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / 'bitcoin' / 'src' / 'bitcoind',
            self.cfg.workdir / 'data',
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.empty_datadir()
        self.client_node.start()
        return self.client_node


class Reindex(IbdBench):
    name = 'reindex'

    def _get_server_node(self):
        return None

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / 'bitcoin' / 'src' / 'bitcoind',
            self.bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.start(reindex=1)
        return self.client_node


class ReindexChainstate(IbdBench):
    name = 'reindex_chainstate'

    def _get_server_node(self):
        return None

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / 'bitcoin' / 'src' / 'bitcoind',
            self.bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.start(**{'reindex-chainstate': 1})
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


def _check_for_ibd_failure(node):
    return node.ps.returncode != 0 or node.check_disk_low()
