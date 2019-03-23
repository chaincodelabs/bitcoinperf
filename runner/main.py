#!/usr/bin/env python3
"""
Run a series of benchmarks against a particular Bitcoin Core revision.

See bin/run_bench for a sample invocation.

"""
import re
import atexit
import os
import subprocess
import tempfile
import datetime
import contextlib
import time
import shlex
import getpass
import logging
import logging.handlers
import traceback
from collections import defaultdict
from pathlib import Path

from . import output, config, endpoints, bitcoind, sh
from .logging import get_logger
from .sh import run, popen

logger = get_logger()

# Global config object; set below in main() after we've parsed commandline
# arguments.
cfg = None

# TODO Actually, commit name -> {bench name -> measurement}.
NAME_TO_TIME = defaultdict(lambda: defaultdict(list))


@contextlib.contextmanager
def timer(name: str):
    start = time.time()
    yield
    NAME_TO_TIME[cfg.run_data.gitref][name].append(time.time() - start)


# Maintain a lockfile that is global across the host to ensure that we're not
# running more than one instance on a given system.
LOCKFILE_PATH = Path("/tmp/bitcoin_bench.lock")


def _drop_caches():
    # N.B.: the host sudoer file needs to be configured to allow non-superusers
    # to run this command. See: https://unix.stackexchange.com/a/168670
    if not cfg.no_caution:
        run("sudo /sbin/sysctl vm.drop_caches=3")


def _startup_assertions():
    """
    Ensure the benchmark environment is suitable in various ways.
    """
    if not cfg.no_caution:
        if run("pgrep --list-name bitcoin | grep -v bitcoinperf",
                check_returncode=False)[2] == 0:
            raise RuntimeError(
                "benchmarks shouldn't run concurrently with unrelated bitcoin "
                "processes")

        if run("$(which time) -f %M sleep 0.01",
                check_returncode=False)[2] != 0:
            raise RuntimeError("the time package is required")

        run('sudo swapoff -a')

        if run('cat /proc/swaps | grep -v "^Filename"',
                check_returncode=False)[2] != 1:
            raise RuntimeError("swap must be disabled during benchmarking")

    if not _try_acquire_lockfile():
        raise RuntimeError(
            "Couldn't acquire lockfile %s; exiting", LOCKFILE_PATH)


def get_commits():
    cfg.commits = list(filter(None, cfg.commits))

    if not cfg.commits:
        return ['HEAD']
    return cfg.commits


def get_times_to_run(bench_name):
    return cfg.run_counts.get(bench_name, 1)


def benchmark(name):
    """A decorator used to declare benchmark steps.

    Handles skipping and count-based execution."""
    def wrapper(func):
        def inner(*args_, **kwargs):
            if name not in cfg.benches_to_run:
                logger.debug("Skipping benchmark %r", name)
            else:
                count = get_times_to_run(name)
                logger.info("Running benchmark %r %d times", name, count)
                # Drop system caches to ensure fair runs.
                _drop_caches()

                for i in range(count):
                    func(*args_, **kwargs)

        return inner
    return wrapper


@benchmark('gitclone')
def bench_gitclone(to_path: Path):
    with timer("gitclone"):
        run("git clone -b {} {} {}".format(
            cfg.repo_branch, cfg.repo_location, to_path))


@benchmark('build')
def bench_build():
    logger.info("Building db4")
    run("./contrib/install_db4.sh .")

    my_env = os.environ.copy()
    my_env['BDB_PREFIX'] = "%s/bitcoin/db4" % cfg.run_data.workdir

    run("./autogen.sh")

    configure_prefix = ''
    if cfg.run_data.compiler == 'clang':
        configure_prefix = 'CC=clang CXX=clang++ '

    # Ensure build is clean.
    makefile_path = cfg.run_data.workdir / 'bitcoin' / 'Makefile'
    if makefile_path.is_file() and not cfg.no_clean:
        run('make distclean')

    boostflags = ''
    armlib_path = '/usr/lib/arm-linux-gnueabihf/'

    if Path(armlib_path).is_dir():
        # On some architectures we need to manually specify this,
        # otherwise configuring with clang can fail.
        boostflags = '--with-boost-libdir=%s' % armlib_path

    logger.info("Running ./configure [...]")
    run(
        configure_prefix +
        './configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" '
        'BDB_CFLAGS="-I${BDB_PREFIX}/include" '
        # Ensure ccache is disabled so that subsequent make runs
        # are timed accurately.
        '--disable-ccache ' + boostflags,
        env=my_env)

    _try_execute_and_report(
        'build.make.%s.%s' % (cfg.make_jobs, cfg.run_data.compiler),
        "make -j %s" % cfg.make_jobs,
        executable='make')


@benchmark('makecheck')
def bench_makecheck():
    _try_execute_and_report(
        'makecheck.%s.%s' % (cfg.run_data.compiler, cfg.nproc - 1),
        "make -j %s check" % (cfg.nproc - 1),
        num_tries=3, executable='make')


@benchmark('functionaltests')
def bench_functests():
    _try_execute_and_report(
        'functionaltests.%s' % cfg.run_data.compiler,
        "./test/functional/test_runner.py",
        num_tries=3, executable='functional-test-runner')


@benchmark('microbench')
def bench_microbench():
    with timer("microbench.%s" % cfg.run_data.compiler):
        _drop_caches()
        microbench_ps = popen("./src/bench/bench_bitcoin")
        (microbench_stdout,
         microbench_stderr) = microbench_ps.communicate()

    if microbench_ps.returncode != 0:
        text = "stdout:\n%s\nstderr:\n%s" % (
            microbench_stdout.decode(), microbench_stderr.decode())

        endpoints.send_to_slack_attachment(
            cfg,
            "Microbench exited with code %s" %
            microbench_ps.returncode, {}, text=text, success=False)

    microbench_lines = [
        # Skip the first line (header)
        i.decode().split(', ')
        for i in microbench_stdout.splitlines()[1:]]

    for line in microbench_lines:
        # Line strucure is
        # "Benchmark, evals, iterations, total, min, max, median"
        assert(len(line) == 7)
        (bench, median, max_, min_) = (
            line[0], float(line[-1]), float(line[-2]), float(line[-3]))
        if not (max_ >= median >= min_):
            logger.warning(
                "%s has weird results: %s, %s, %s" %
                (bench, max_, median, min_))
            assert False
        endpoints.send_to_codespeed(
            cfg,
            "micro.%s.%s" % (cfg.run_data.compiler, bench),
            median, 'bench-bitcoin', result_max=max_, result_min=min_)


@benchmark('ibd')
def bench_ibd():
    bench_prefix = 'ibd.real' if cfg.ibd_from_network else 'ibd.local'
    bench_name_fmt = bench_prefix + '.{}.dbcache=' + cfg.bitcoind_dbcache
    checkpoints = list(sorted(
        int(i) for i in cfg.ibd_checkpoints.replace("_", "").split(",")))

    bitcoind.empty_datadir(cfg.run_data.workdir / 'bitcoin')

    def report_checkpoint_to_codespeed(command, height):
        # Report to codespeed for this blockheight checkpoint
        cmd.report_to_codespeed(
            cfg, 'bitcoind',
            name=bench_name_fmt.format(height),
            extra_data={
                'height': height,
                'dbcache': cfg.bitcoind_dbcache,
            },
        )

    with bitcoind.run_synced_bitcoind(cfg):
        cmd = IBDCommand.from_cfg(bench_name_fmt.format('tip'))
        cmd.start()

        failure_count = 0
        last_height_seen = 1
        next_checkpoint = checkpoints.pop(0) if checkpoints else None

        while True:
            info = bitcoind.call_rpc(cfg, "getblockchaininfo")

            if not info:
                failure_count += 1
                if failure_count > 20:
                    logger.error(
                        "Bitcoind hasn't responded to RPC in a suspiciously "
                        "long time... hung?")
                    break
                time.sleep(2)
                continue

            last_height_seen = info['blocks']
            logger.debug("Saw height %s", last_height_seen)

            if next_checkpoint and \
                    next_checkpoint != 'tip' and \
                    last_height_seen >= next_checkpoint:
                # Report to codespeed for this blockheight checkpoint
                report_checkpoint_to_codespeed(cmd, next_checkpoint)
                next_checkpoint = checkpoints.pop(0) if checkpoints else None

                if not next_checkpoint:
                    logger.debug("Out of checkpoints - shutting down the ibd")
                    break

            if cmd.returncode is not None or \
                    info["verificationprogress"] > 0.9999:
                logger.debug("IBD complete or failed: %s", info)
                break

            time.sleep(5)

        cmd.join()

        if not cmd.check_for_failure():
            for height in checkpoints:
                report_checkpoint_to_codespeed(cmd, height)

            cmd.report_to_codespeed(cfg, 'bitcoind', extra_data={
                'height': last_height_seen,
                'dbcache': cfg.bitcoind_dbcache,
            })


@benchmark('reindex')
def bench_reindex():
    bench_name = 'reindex.%s.dbcache=%s' % (
        cfg.bitcoind_stopatheight, cfg.bitcoind_dbcache),

    cmd = IBDCommand.from_cfg(bench_name, reindex=True)
    cmd.start()
    cmd.join()

    if not cmd.check_for_failure():
        cmd.report_to_codespeed(cfg, 'bitcoind')


def run_benches():
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    logger.info(
        "Running benchmarks %s with compilers %s",
        cfg.benches_to_run, cfg.compilers)

    _startup_assertions()

    cfg.run_data.workdir = Path(
        cfg.workdir or tempfile.mkdtemp(prefix=cfg.bench_prefix))
    cfg.run_data.gitref = cfg.repo_branch

    bench_gitclone(cfg.run_data.src_dir)
    os.chdir(str(cfg.run_data.src_dir))

    for commit in get_commits():
        if commit != 'HEAD':
            logger.info("Checking out commit %s", commit)
            run("git checkout %s" % commit)

        cfg.run_data.gitref = commit
        cfg.run_data.gitsha = subprocess.check_output(
            shlex.split('git rev-parse HEAD')).strip().decode()

        for compiler in cfg.compilers:
            cfg.run_data.compiler = compiler
            bench_build()
            bench_makecheck()
            bench_functests()
            bench_microbench()

        bench_ibd()
        bench_reindex()


def _try_acquire_lockfile():
    if LOCKFILE_PATH.exists():
        return False

    with LOCKFILE_PATH.open('w') as f:
        f.write("%s,%s" % (datetime.datetime.utcnow(), getpass.getuser()))
    cfg.run_data.lockfile_acquired = True
    return True


def _clean_shutdown():
    # Release lockfile if we've got it
    if cfg.run_data.lockfile_acquired:
        LOCKFILE_PATH.unlink()
        logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

    # Clean up to avoid filling disk
    if cfg.run_data.workdir and \
            not cfg.no_teardown and \
            cfg.run_data.workdir.is_dir():

        os.chdir(str(cfg.run_data.workdir / ".."))
        _stash_debug_file()
        run("rm -rf %s" % cfg.run_data.workdir)
        logger.debug("shutdown: removed workdir at %s", cfg.run_data.workdir)
    elif cfg.no_teardown:
        logger.debug("shutdown: leaving workdir at %s", cfg.run_data.workdir)


def _stash_debug_file():
    # Move the debug.log file out into /tmp for diagnostics.
    debug_file = cfg.run_data.workdir / "/bitcoin/data/debug.log"
    if debug_file.is_file():
        # Overwrite the file so as not to fill up disk.
        debug_file.rename(Path("/tmp/bench-debug.log"))


class Command:
    """
    Manages the running of a subprocess for a certain benchmark.
    """
    def __init__(self, cmd: str, bench_name: str):
        self.cmd = cmd
        self.bench_name = bench_name
        self.ps = None
        self.start_time = None
        self.end_time = None
        self.stdout = None
        self.stderr = None

    def start(self):
        self.start_time = time.time()
        self.ps = popen('$(which time) -f %M ' + self.cmd)
        logger.info("[%s] command '%s' starting", self.bench_name, self.cmd)

    def join(self):
        (self.stdout, self.stderr) = self.ps.communicate()
        self.end_time = time.time()

    @property
    def total_secs(self) -> float:
        return (self.end_time or time.time()) - self.start_time

    @property
    def returncode(self):
        return self.ps.returncode

    @property
    def memusage_kib(self) -> int:
        if self.returncode is None:
            return sh.get_proc_peakmem_kib(self.ps)
        return int(self.stderr.decode().strip().split('\n')[-1])

    def check_for_failure(self):
        """
        Parse output and returncode to determine if there was a failure.

        Sometimes certain benchmarks may fail with zero returncodes and we must
        check other things to detect the failure.
        """
        failed = False

        if self.returncode is None:
            raise RuntimeError("can't check for failure before completion")

        if re.match(r'ibd.*|reindex', self.bench_name):
            disk_warning_ps = subprocess.run(
                ("tail -n 10000 {}/bitcoin/data/debug.log | "
                 "grep 'Disk space is low!' ").format(cfg.run_data.workdir),
                shell=True)

            if disk_warning_ps.returncode == 0:
                logger.warning(
                    "Ran out of disk space while running benchmark %s" %
                    self.bench_name)
                failed = True

        if re.match(r'ibd.*', self.bench_name):
            one_hour_secs = 60 * 60 * 2

            if self.total_secs < one_hour_secs:
                logger.warning("IBD finished implausibly quickly")
                # failed = True

        if self.returncode != 0:
            failed = True

        if failed:
            logger.error(
                "[%s] command failed\nstdout:\n%s\nstderr:\n%s",
                self.bench_name,
                self.stdout.decode()[-10000:],
                self.stderr.decode()[-10000:])
        else:
            logger.info(
                "[%s] command finished successfully in %.3f seconds (%s) "
                "with maximum resident set size %.3f MiB",
                self.bench_name, self.total_secs,
                datetime.timedelta(seconds=self.total_secs),
                self.memusage_kib / 1024)

        return failed

    def report_to_codespeed(self,
                            cfg,
                            executable: str,
                            name: str = None,
                            extra_data: dict = None):
        name = name or self.bench_name

        NAME_TO_TIME[cfg.run_data.gitref][name].append(self.total_secs)
        endpoints.send_to_codespeed(
            cfg, name, self.total_secs, executable,
            extra_data=extra_data,
        )

        # This may be called before the command has completed (in the case of
        # incremental IBD reports), so only report memory usage if we have
        # access to it.
        if self.memusage_kib is not None:
            mem_name = name + '.mem-usage'
            NAME_TO_TIME[cfg.run_data.gitref][mem_name].append(
                self.memusage_kib)
            endpoints.send_to_codespeed(
                cfg, mem_name, self.memusage_kib, executable,
                units_title='Size', units='KiB')


class IBDCommand(Command):

    def __init__(self,
                 bench_name,
                 dbcache=None,
                 txindex=1,
                 assumevalid=None,
                 stopatheight=None,
                 reindex=False,
                 ):
        self.dbcache = dbcache
        self.txindex = txindex
        self.assumevalid = assumevalid
        self.stopatheight = stopatheight

        connect_config = '-listen=0' if cfg.ibd_from_network else '-connect=0'
        addnode_config = (
            # If we aren't IBDing from random peers on the network, specify the
            # peer.
            ('-addnode=%s' % cfg.ibd_peer_address)
            if not cfg.ibd_from_network else '')

        run_bitcoind_cmd = (
            './src/bitcoind -datadir={}/bitcoin/data '
            '-dbcache={} -rpcuser=foo -rpcpassword=bar -txindex=1 '
            '{} -debug=all -assumevalid={} '
            '-port={} -rpcport={} {} {}'.format(
                cfg.run_data.workdir,
                dbcache,
                connect_config,
                assumevalid,
                cfg.bitcoind_port,
                cfg.bitcoind_rpcport,
                addnode_config,
                config.BENCH_SPECIFIC_BITCOIND_ARGS,
            ))

        if stopatheight:
            run_bitcoind_cmd += " -stopatheight={}".format(stopatheight)

        if reindex:
            run_bitcoind_cmd += " -reindex"

        super().__init__(run_bitcoind_cmd, bench_name)

    @classmethod
    def from_cfg(cls, bench_name, **kwargs):
        return cls(
            bench_name,
            dbcache=cfg.bitcoind_dbcache,
            assumevalid=cfg.bitcoind_assumevalid,
            **kwargs,
        )

    def join(self):
        """
        When stopatheight isn't set, bitcoind will run for perpetuity unless
        we stop it.
        """
        if not self.stopatheight:
            bitcoind.stop_via_rpc(cfg, self.ps)
        super().join()


def _try_execute_and_report(
        bench_name, cmd, *, num_tries=1, executable='bitcoind'):
    """
    Attempt to execute some command a number of times and then report
    its execution memory usage or execution time to codespeed over HTTP.
    """
    for i in range(num_tries):
        cmd = Command(cmd, bench_name)
        cmd.start()
        cmd.join()

        if not cmd.check_for_failure():
            # Command succeeded
            break

        if i == (num_tries - 1):
            return False

    cmd.report_to_codespeed(cfg, executable)


class SlackLogHandler(logging.Handler):
    def emit(self, record):
        fmtd = self.format(record)

        # If the log is multiple lines, treat the first line as the title and
        # the remainder as text.
        title, *rest = fmtd.split('\n', 1)
        return endpoints.send_to_slack_attachment(
            cfg, title, {}, text=(rest[0] if rest else None), success=False)


def attach_slack_handler_to_logger(logger):
    """Can't do this in .logging because we need a cfg argument."""
    slack = SlackLogHandler()
    slack.setLevel(logging.WARNING)
    slack.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(slack)


def main():
    global cfg
    global logger
    cfg = config.parse_args()

    atexit.register(_clean_shutdown)
    attach_slack_handler_to_logger(logger)

    logger.info("Running with configuration:")
    logger.info("")
    for name, val in sorted(cfg.__dict__.items()):
        logger.info("  {0:<26} {1:<40}".format(name, str(val)))
    logger.info("")

    try:
        run_benches()

        if len(get_commits()) <= 1:
            timestr = output.get_times_table(
                NAME_TO_TIME[cfg.run_data.gitref])
            print(timestr)
        else:
            output.print_comparative_times_table(NAME_TO_TIME)
    except Exception:
        endpoints.send_to_slack_attachment(
            cfg, "Error", {}, text=traceback.format_exc(), success=False)
        raise


if __name__ == '__main__':
    main()
