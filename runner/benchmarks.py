import abc
import copy
import time
import datetime
import shutil
import typing as t
from pathlib import Path

from . import bitcoind, results, sh, config, hwinfo, logparse
from .globals import G
from .logging import get_logger
from .sh import popen
from .results import HeightData

logger = get_logger()


class Benchmark(abc.ABC):
    name: str = ""

    # Each benchmark has a `Results` class associated with it that indicates
    # what data the benchmark run will generate. These classes are defined
    # in the `runner.results` module.
    #
    # This type is used to instantiate `self.results`, which should be filled
    # out during bench runtime.
    _results_class: t.Type[results.Results] = results.Results

    def __init__(
        self,
        cfg: config.Config,
        bench_cfg: config.Bench,
        compiler: config.Compilers,
        target: config.Target,
        run_idx: int = 0,
    ):
        self.cfg = cfg
        self.run_idx = run_idx
        self.bench_cfg = bench_cfg
        self.compiler = compiler
        assert target.gitco
        self.gitco: config.GitCheckout = copy.copy(target.gitco)
        self.target = target
        self.id: str = self.id_format.format(
            self=self, cfg=cfg, G=G, bench_cfg=self.bench_cfg
        )

        # Each subclass must define a Results class, which defines the schema
        # of result data that the bench run will yield.
        #
        # This result data is cached in-memory and retained with each
        # `Benchmark` instance for post-processing in the `runner.output`
        # module.
        self.results = self._results_class()

    def __getstate__(self):
        state = self.__dict__.copy()
        for attr in ("client_node", "server_node"):
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

    @property
    def artifacts_dir(self) -> Path:
        """A place to stash various artifacts from the benchmark."""
        assert self.cfg.workdir

        if not getattr(self, "_artifacts_dir", None):
            prefix = f"artifacts-{self.id}-{self.gitco.ref}"
            idx = len(list(self.cfg.workdir.glob(prefix + "*")))

            if idx != self.run_idx:
                logger.warning(
                    "Unexpected drift in run index from artifacts index "
                    f"({prefix}: got {idx}, expected {self.run_idx})"
                )

            path = self.cfg.workdir / (prefix + f".{idx}")
            path.mkdir(parents=True)
            self._artifacts_dir = path
        return self._artifacts_dir

    def run(self, cfg, bench_cfg) -> None:
        """Called externally."""
        sh.drop_caches()

        G.benchmark = self.__class__

        logger.info("[%s] starting", self.id or self.name)
        try:
            self._run(cfg, bench_cfg)
        except Exception:
            logger.exception("[%s] failed with an exception", self.id or self.name)
            raise
        finally:
            # FWIW, on Ctrl+C teardown is unconditionally handled by
            # `main._get_shutdown_handler`. Running this again shouldn't
            # cause a problem though.
            self._teardown()

        logger.info("[%s] done", self.id or self.name)

    def _try_execute_and_report(self, cmd_str, *, num_tries=1):
        """
        Attempt to execute some command a number of times and then report
        its execution memory usage or execution time to codespeed over HTTP.
        """
        for i in range(num_tries):
            cmd = sh.Command(cmd_str, self.name)
            cmd.start()
            cmd.join()
            self._report_results(cmd)
            if not cmd.check_for_failure():
                return True

        return False

    def _report_results(self, cmd: sh.Command):
        if cmd.check_for_failure():
            self._log_result(False, self.id, cmd)
        else:
            self._log_result(True, self.id, cmd)
            self.results.command = cmd.cmd
            self.results.total_time_secs = int(cmd.total_secs)
            self.results.peak_rss_kb = cmd.memusage_kib()
            self.results.cpu_kernel_secs = cmd.cpu_kernel_secs()
            self.results.cpu_user_secs = cmd.cpu_user_secs()

            assert self.cfg.workdir
            self.results.configure_info = hwinfo.parse_configure_log(
                self.cfg.workdir / "bitcoin"
            )

            results.report_result(self, self.id, cmd.total_secs)
            results.report_result(self, self.id + ".mem-usage", cmd.memusage_kib())

    def _log_result(self, succeeded: bool, bench_name: str, cmd: sh.Command):
        if not succeeded:
            assert cmd.stdout is not None
            assert cmd.stderr is not None

            logger.error(
                "[%s] command failed with code %d\nstdout:\n%s\nstderr:\n%s",
                bench_name,
                cmd.returncode,
                cmd.stdout.decode()[-10000:],
                cmd.stderr.decode()[-10000:],
            )
        else:
            logger.info(
                "[%s] command finished successfully in %.3f seconds (%s) "
                "with maximum resident set size %.3f MiB",
                bench_name,
                cmd.total_secs,
                datetime.timedelta(seconds=cmd.total_secs),
                cmd.memusage_kib() / 1024,
            )


class Build(Benchmark):
    name = "build"
    id_format = "build.make.{bench_cfg.num_jobs}.{self.compiler}"

    def _run(self, cfg, bench_cfg):
        sh.cd(cfg.workdir)
        num_jobs = bench_cfg.num_jobs
        builder = bitcoind.BuildManager(
            cfg.workdir,
            cfg.build_cache_path(),
            clean=cfg.clean,
        )
        self.results.title = f"Build with {self.compiler} (j={num_jobs})"
        cmd = builder.build(
            self.target,
            self.compiler,
            num_jobs=bench_cfg.num_jobs,
            copy_log_to=self.artifacts_dir,
        )
        if cmd:  # i.e. if not cached
            self._report_results(cmd)
            raise RuntimeError(
                f"{self.target} failed to build with {self.compiler} "
                f"({self.artifacts_dir})")

        shutil.copyfile(
            builder.repo_path / "config.log", self.artifacts_dir / "config.log"
        )
        logger.info("Configure log saved in %s", self.artifacts_dir / "config.log")


class MakeCheck(Benchmark):
    name = "makecheck"
    id_format = "makecheck.{self.compiler}.j={bench_cfg.num_jobs}"

    def _run(self, cfg, bench_cfg):
        cmd = f"make -j {bench_cfg.num_jobs} check"
        self.results.title = f"Make check (j={bench_cfg.num_jobs})"
        self._try_execute_and_report(cmd, num_tries=3)


class FunctionalTests(Benchmark):
    name = "functests"
    id_format = "functionaltests.{self.compiler}.j={bench_cfg.num_jobs}"

    def _run(self, cfg, bench_cfg):
        cmd = "./test/functional/test_runner.py"
        self.results.title = "Functional tests"
        self._try_execute_and_report(cmd, num_tries=3)


class Microbench(Benchmark):
    name = "microbench"
    id_format = "micro.{self.compiler}"
    _results_class = results.MicrobenchResults

    def _run(self, cfg, bench_cfg):
        time_start = time.time()
        sh.drop_caches()
        cmd_str = "./src/bench/bench_bitcoin"

        if bench_cfg.filter:
            cmd_str += " -filter='{}'".format(bench_cfg.filter)

        outpath = self.artifacts_dir / f"{self.id}_results"
        # TODO: use sh.Command, report peak memory usage - maybe per bench?
        cmd_str += f" -output_csv={outpath} > /dev/null && cat {outpath}"

        microbench_ps = popen(cmd_str)
        (microbench_stdout, microbench_stderr) = microbench_ps.communicate()
        self.results.command = cmd_str
        self.results.title = "Microbench"
        self.results.total_time_secs = time.time() - time_start

        # Don't use _try_execute_and_report because we need to report each
        # microbenchmark individually.

        if microbench_ps.returncode != 0:
            text = "stdout:\n%s\nstderr:\n%s" % (
                microbench_stdout.decode(),
                microbench_stderr.decode(),
            )

            msg = "Microbench exited with code %s" % microbench_ps.returncode
            if G.slack:
                G.slack.send_to_slack_attachment(
                    self.gitco, msg, {}, text=text, success=False
                )
            else:
                logger.warning(f"{msg} on {self.gitco}:\n{text}")

        microbench_lines = [
            # Skip the first line (header)
            i.decode().split(", ")
            for i in microbench_stdout.splitlines()[1:]
        ]

        for line in microbench_lines:
            # Line strucure is
            # "Benchmark, evals, iterations, total, min, max, median"
            assert len(line) == 7
            (bench, median, max_, min_) = (
                line[0],
                float(line[-1]),
                float(line[-2]),
                float(line[-3]),
            )
            if not max_ >= median >= min_:
                logger.warning(
                    "%s has weird results: %s, %s, %s" % (bench, max_, median, min_)
                )
                assert False
            self.results.bench_to_time[bench] = median
            results.report_result(
                self,
                "micro.{compiler}.{bench}".format(compiler=self.compiler, bench=bench),
                median,
                extra_data={"result_max": max_, "result_min": min_},
            )


class _IbdBench(Benchmark):
    """
    This is an abstract class that unifies common code for IBD-like benchmarks,
    which includes reindexing.
    """

    name = "ibd"
    _results_class = results.IbdResults
    id_format = ""  # We use _get_codespeed_bench_name() instead.

    def __init__(self, *args, **kwargs):
        self.client_node = self.server_node = None
        super().__init__(*args, **kwargs)
        self.id = self._get_codespeed_bench_name(self.bench_cfg.end_height or "tip")

    def _get_server_node(self) -> t.Optional[bitcoind.Node]:
        # This might return None if we're IBDing from network.
        peer = self.cfg.synced_peer

        if not peer:
            logger.info("running benchmark without synced peer")
            return None
        elif peer.address:
            logger.info(f"using networked synced peer at {peer.address}")
            return None

        self.server_node = bitcoind.get_synced_node(peer)
        return self.server_node

    def _get_dbcache(self) -> str:
        assert self.client_node.cmd
        for i in self.client_node.cmd.cmd.split():
            if i.startswith("-dbcache="):
                return i.split("=")[-1]
        return "500"  # The default dbcache value at time of writing

    def _get_client_node(self) -> bitcoind.Node:
        pass

    def _get_codespeed_bench_name(self, current_height) -> str:
        assert isinstance(self.bench_cfg, config.IBDishBench)

        if self.bench_cfg.start_height:
            fmt = "{self.name}.{start_height}.{current_height}"
        else:
            fmt = "{self.name}.{current_height}"

        return fmt.format(
            self=self,
            current_height=current_height,
            start_height=self.bench_cfg.start_height,
        )

    def _run(self, cfg, bench_cfg):
        self.server_node = self._get_server_node()
        self.client_node = client_node = self._get_client_node()

        starting_height = client_node.wait_for_init()
        last_height_seen = starting_height
        last_resource_usage = None
        start_time = None

        self.results.command: str = self.client_node.cmd.cmd
        self.results.title: str = self._get_title()

        if self.server_node:
            server_blockchaininfo = self.server_node.call_rpc("getblockchaininfo")
            client_blockchaininfo = self.client_node.call_rpc("getblockchaininfo")
            server_blocks = server_blockchaininfo["blocks"]
            client_headers = client_blockchaininfo["headers"]

            if server_blocks < bench_cfg.end_height:
                raise RuntimeError(
                    (
                        "Server blocks ({}) must be greater than end height "
                        "({}) otherwise the IBD will stall. "
                        "Sync the server's datadir to a height past {}."
                    ).format(server_blocks, bench_cfg.end_height, bench_cfg.end_height)
                )

            if server_blocks <= client_headers:
                raise RuntimeError(
                    (
                        "Server blocks ({}) must be greater than client headers "
                        "({}) otherwise the IBD will stall since the server "
                        "cannot report a connected block better than the "
                        "client's existing header chain. "
                        "Sync the server's datadir to a height past {}."
                    ).format(server_blocks, client_headers, client_headers)
                )

        extra_data = {
            "start_height": bench_cfg.start_height,
            **client_node.get_args_dict(),
        }

        report_to_codespeed_heights: t.List[int] = list(bench_cfg.time_heights or [])
        iters = 0
        time_now = None

        # Poll the running bitcoind process for its current height and report
        # results whenever we've crossed one of the user-specific checkpoints.
        #
        while True:
            if client_node.ps.returncode is not None:
                logger.info("node process died: %s", client_node)
                break

            (last_height_seen, progress) = client_node.poll_for_height_and_progress()

            logger.debug("Last saw height=%s progress=%s", last_height_seen, progress)

            if last_height_seen is None and progress is None:
                raise RuntimeError("RPC calls to {} failed".format(client_node))

            elif (
                bench_cfg.end_height and last_height_seen >= bench_cfg.end_height
            ) or progress > 0.9999:
                # Be sure we've set start time in case the bench finished
                # really fast.
                start_time = start_time or time.time()

                logger.info(
                    "ending IBD based on height (%s) or progress (%s)",
                    last_height_seen,
                    progress,
                )
                break

            elif last_height_seen < bench_cfg.start_height:
                logger.debug(
                    "height (%s) not yet at min height %s",
                    last_height_seen,
                    bench_cfg.start_height,
                )
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
            while (
                report_to_codespeed_heights
                and report_to_codespeed_heights[0] <= last_height_seen
            ):
                report_at_height = report_to_codespeed_heights.pop(0)
                results.report_result(
                    self,
                    self._get_codespeed_bench_name(report_at_height),
                    time_now,
                    extra_data={"height": last_height_seen, **extra_data},
                )
                results.report_result(
                    self,
                    self._get_codespeed_bench_name(report_at_height) + ".mem-usage",
                    client_node.cmd.memusage_kib(),
                    extra_data={"height": last_height_seen, **extra_data},
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
                    "Last saw height=%s progress=%s", last_height_seen, progress
                )
                logger.debug(
                    "Codespeed checkpoints left: %s", report_to_codespeed_heights
                )

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
        if client_node.ps.returncode != 0 or client_node.check_disk_low():
            logger.info("IBD failed")
            self._log_result(False, final_name, self.client_node.cmd)
            return False

        self._log_result(True, final_name, self.client_node.cmd)

        # Mark measurements for all heights remaining.
        #
        while (
            report_to_codespeed_heights
            and report_to_codespeed_heights[0] <= last_height_seen
        ):
            report_at_height = report_to_codespeed_heights.pop(0)
            results.report_result(
                self,
                self._get_codespeed_bench_name(report_at_height),
                # time_now is None if command completed before a single
                # measurement.
                time_now or 0,
                extra_data={"height": last_height_seen, **extra_data},
            )
            results.report_result(
                self,
                self._get_codespeed_bench_name(report_at_height) + ".mem-usage",
                client_node.cmd.memusage_kib(),
                extra_data={"height": last_height_seen, **extra_data},
            )

        # Record the time-to-tip if we didn't specify an end height.
        #
        if progress > 0.999 and not bench_cfg.end_height:
            results.report_result(
                self,
                self._get_codespeed_bench_name("tip"),
                final_time,
                extra_data={"height": last_height_seen, **extra_data},
            )
            results.report_result(
                self,
                self._get_codespeed_bench_name("tip") + ".mem-usage",
                client_node.cmd.memusage_kib(),
                extra_data={"height": last_height_seen, **extra_data},
            )

        self.results.total_time_secs = final_time
        self.results.peak_rss_kb = self.client_node.cmd.memusage_kib()
        self.results.cpu_kernel_secs = self.client_node.cmd.cpu_kernel_secs()
        self.results.cpu_user_secs = self.client_node.cmd.cpu_user_secs()

        if last_resource_usage:
            self.results.height_to_data[last_height_seen] = HeightData(
                final_time,
                last_resource_usage.rss_kb,
                last_resource_usage.cpu_percent,
                last_resource_usage.num_fds,
            )

    def _get_datadir_path(self) -> Path:
        assert self.client_node.cmd
        cmd: str = self.client_node.cmd.cmd
        assert "-datadir=" in cmd

        for i in cmd.split():
            if i.startswith("-datadir="):
                return Path(i.split("=", 1)[-1])
        raise RuntimeError(f"no datadir extractable from {cmd}")

    def _teardown(self):
        """
        Shut down all the nodes we started and stash the datadir if need be.
        """
        if not self.client_node:
            return
        if self.client_node.is_process_alive:
            # Longer timeout - might be flushing cache
            self.client_node.stop_via_rpc(timeout=(60 * 25))
        if self.server_node:
            self.server_node.stop_via_rpc(timeout=120)

        # Copy logfile to results
        datadirpath = self._get_datadir_path()
        debuglogpath = self._get_datadir_path() / "debug.log"
        if debuglogpath.exists():
            shutil.copyfile(debuglogpath, str(self.artifacts_dir / "debug.log"))

            with open(debuglogpath, "r") as f:
                self.results.flush_events = logparse.get_flush_times(f)

        if getattr(self.bench_cfg, "stash_datadir", None):
            src_datadir = getattr(self.bench_cfg, "src_datadir", None)

            # If the src_datadir is the same one that we'll stash to,
            # leave it in place.
            if src_datadir == self.bench_cfg.stash_datadir:
                return

            if self.bench_cfg.stash_datadir.exists():
                logger.warning(
                    "removing existing stash_datadir (%s)", self.bench_cfg.stash_datadir
                )
                sh.rm(self.bench_cfg.stash_datadir)

            shutil.move(datadirpath, self.bench_cfg.stash_datadir)
            logger.info(
                "Stashed datadir from %s -> %s",
                datadirpath,
                self.bench_cfg.stash_datadir,
            )
        else:
            if datadirpath.exists():
                logger.info(f"removing datadir at {datadirpath}")
                shutil.rmtree(datadirpath)


class IbdLocal(_IbdBench):
    name = "ibd.local"

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / "bitcoin",
            self.cfg.workdir / "data",
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.empty_datadir()

        self.client_node.start(
            **{
                "listen": 0,
                "connect": 0,
                "addnode": (
                    "127.0.0.1:{}".format(self.server_node.port)
                    if self.server_node
                    else self.cfg.synced_peer.address
                ),
            }
        )

        return self.client_node

    def _get_title(self):
        return "IBD from on-host peer to height {} (dbcache={})".format(
            self.bench_cfg.end_height, self._get_dbcache()
        )


class IbdRangeLocal(_IbdBench):
    name = "ibd.local.range"  # Range is reflected in starting height

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / "bitcoin",
            self.cfg.workdir / "data",
            copy_from_datadir=self.bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        # Don't empty datadir since we just copied it from a pruned source.

        self.client_node.start(
            **{
                "listen": 0,
                "connect": 0,
                "addnode": (
                    "127.0.0.1:{}".format(self.server_node.port)
                    if self.server_node
                    else self.cfg.synced_peer.address
                ),
                # Set an unreasonably high prune target so that we can resume from
                # a pruned datadir but never actually prune.
                "prune": 9999999,
            }
        )
        return self.client_node

    def _get_title(self):
        return "IBD from on-host peer, heights {}-{} (dbcache={})".format(
            self.bench_cfg.start_height, self.bench_cfg.end_height, self._get_dbcache()
        )


class IbdReal(_IbdBench):
    name = "ibd.real"

    def _get_server_node(self):
        return None

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / "bitcoin",
            self.cfg.workdir / "data",
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.empty_datadir()
        self.client_node.start()
        return self.client_node

    def _get_title(self):
        return "IBD from the live network to height {} (dbcache={})".format(
            self.bench_cfg.end_height, self._get_dbcache()
        )


class Reindex(_IbdBench):
    name = "reindex"

    def _get_server_node(self):
        return None

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / "bitcoin",
            self.bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.start(reindex=1)
        return self.client_node

    def _get_title(self):
        return "Reindex to height {} (dbcache={})".format(
            self.bench_cfg.end_height, self._get_dbcache()
        )


class ReindexChainstate(_IbdBench):
    name = "reindex_chainstate"

    def _get_server_node(self):
        return None

    def _get_client_node(self):
        self.client_node = bitcoind.Node(
            self.cfg.workdir / "bitcoin",
            self.bench_cfg.src_datadir,
            extra_args=self.target.bitcoind_extra_args,
        )

        self.client_node.start(**{"reindex-chainstate": 1})
        return self.client_node

    def _get_title(self):
        return "Reindex-chainstate to height {} (dbcache={})".format(
            self.bench_cfg.end_height, self._get_dbcache()
        )
