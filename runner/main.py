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
import json
import datetime
import contextlib
import time
import shlex
import getpass
import traceback
from collections import defaultdict
from pathlib import Path

from . import output, logging, config, endpoints


# Global config object; set below in main() after we've parsed commandline
# arguments.
cfg = None
logger = None

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


BENCH_SPECIFIC_BITCOIND_ARGS = (
    # To "complete" (i.e. latch false out of) initialblockdownload for
    # stopatheight for a lowish height, we need to set a very large maxtipage.
    '-maxtipage=99999999999999999999 '

    # If we don't set minimumchainwork to 0, low heights may cause the syncing
    # peer to never download blocks and thus hang indefinitely during IBD.
    # See https://github.com/bitcoin/bitcoin/blob/e83d82a85c53196aff5b5ac500f20bb2940663fa/src/net_processing.cpp#L517-L521  # noqa
    '-minimumchainwork=0x00 '

    # Output buffering into memory during ps.communicate() can cause OOM errors
    # on machines with small memory, so only output to debug.log files in disk.
    '-printtoconsole=0 '
)


@contextlib.contextmanager
def run_synced_bitcoind():
    """
    Context manager which spawns (and cleans up) a bitcoind instance that has a
    synced chain high enough to service an IBD up to BITCOIND_STOPATHEIGHT.
    """
    if not cfg.running_synced_bitcoind_locally:
        # If we're not running a node locally, don't worry about setup and
        # teardown.
        yield
        return

    bitcoinps = _popen(
        # Relies on bitcoind being precompiled and synced chain data existing
        # in /bitcoin_data; see runner/Dockerfile.
        "%s/src/bitcoind -datadir=%s -noconnect -listen=1 %s %s" % (
            cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir,
            BENCH_SPECIFIC_BITCOIND_ARGS, cfg.synced_bitcoind_args,
            ))

    logger.info(
        "started synced node with '%s' (pid %s)",
        bitcoinps.args, bitcoinps.pid)

    # Wait for bitcoind to come up.
    num_tries = 100
    sleep_time_secs = 2
    bitcoind_up = False

    def stop_synced_bitcoind():
        _run("%s/src/bitcoin-cli -datadir=%s stop" %
             (cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir))
        bitcoinps.wait(timeout=120)

    while num_tries > 0 and bitcoinps.returncode is None and not bitcoind_up:
        info = None
        info_call = _run(
            "{}/src/bitcoin-cli -datadir={} getblockchaininfo".format(
                cfg.synced_bitcoin_repo_dir, cfg.synced_data_dir),
            check_returncode=False)

        if info_call[2] == 0:
            info = json.loads(info_call[0].decode())
        else:
            logger.debug(
                "non-zero returncode (%s) from synced bitcoind status check",
                info_call[2])

        if info and info["blocks"] < int(cfg.bitcoind_stopatheight):
            stop_synced_bitcoind()  # Stop process; we're exiting.
            raise RuntimeError(
                "synced bitcoind node doesn't have enough blocks "
                "(%s vs. %s)" %
                (info['blocks'], int(cfg.bitcoind_stopatheight)))
        elif info:
            bitcoind_up = True
        else:
            num_tries -= 1
            time.sleep(sleep_time_secs)

    if not bitcoind_up:
        raise RuntimeError("Couldn't bring synced node up")

    logger.info("synced node is active (pid %s) %s", bitcoinps.pid, info)

    try:
        yield
    finally:
        logger.info("shutting down synced node (pid %s)", bitcoinps.pid)
        stop_synced_bitcoind()

        if bitcoinps.returncode != 0:
            logger.warning(
                "synced bitcoind returned with nonzero return code "
                "%s" % bitcoinps.returncode)


def _drop_caches():
    # N.B.: the host sudoer file needs to be configured to allow non-superusers
    # to run this command. See: https://unix.stackexchange.com/a/168670
    if not cfg.no_caution:
        _run("sudo /sbin/sysctl vm.drop_caches=3")


def _startup_assertions():
    """
    Ensure the benchmark environment is suitable in various ways.
    """
    if not cfg.no_caution:
        if _run("pgrep --list-name bitcoin | grep -v bitcoinperf",
                check_returncode=False)[2] == 0:
            raise RuntimeError(
                "benchmarks shouldn't run concurrently with unrelated bitcoin "
                "processes")

        if _run("$(which time) -f %M sleep 0.01",
                check_returncode=False)[2] != 0:
            raise RuntimeError("the time package is required")

        _run('sudo swapoff -a')

        if _run('cat /proc/swaps | grep -v "^Filename"',
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
def bench_gitclone():
    with timer("gitclone"):
        _run("git clone -b %s %s" % (cfg.repo_branch, cfg.repo_location))


@benchmark('build')
def bench_build():
    _run("./contrib/install_db4.sh .")

    my_env = os.environ.copy()
    my_env['BDB_PREFIX'] = "%s/bitcoin/db4" % cfg.run_data.workdir

    _run("./autogen.sh")

    configure_prefix = ''
    if cfg.run_data.compiler == 'clang':
        configure_prefix = 'CC=clang CXX=clang++ '

    # Ensure build is clean.
    makefile_path = cfg.run_data.workdir / 'bitcoin' / 'Makefile'
    if makefile_path.is_file() and not cfg.no_clean:
        _run('make distclean')

    boostflags = ''
    armlib_path = '/usr/lib/arm-linux-gnueabihf/'

    if Path(armlib_path).is_dir():
        # On some architectures we need to manually specify this,
        # otherwise configuring with clang can fail.
        boostflags = '--with-boost-libdir=%s' % armlib_path

    _run(
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
        microbench_ps = _popen("./src/bench/bench_bitcoin")
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
def bench_ibd_and_reindex(run_bitcoind_cmd):
    ibd_bench_name = 'ibd.real' if cfg.ibd_from_network else 'ibd.local'

    # Ensure empty data before each IBD.
    datadir = cfg.run_data.workdir / 'bitcoin' / 'data'
    _run("rm -rf %s" % datadir, check_returncode=False)
    if not datadir.exists():
        datadir.mkdir()

    with run_synced_bitcoind():
        _try_execute_and_report(
            '%s.%s.dbcache=%s' % (
                ibd_bench_name,
                cfg.bitcoind_stopatheight, cfg.bitcoind_dbcache),
            run_bitcoind_cmd,
        )

    if 'reindex' in cfg.benches_to_run:
        _try_execute_and_report(
            'reindex.%s.dbcache=%s' % (
                cfg.bitcoind_stopatheight, cfg.bitcoind_dbcache),
            '%s -reindex' % run_bitcoind_cmd)


def run_benches():
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    logger.info(
        "Running benchmarks %s with compilers %s",
        cfg.benches_to_run, cfg.compilers)

    _startup_assertions()

    if cfg.workdir:
        workdir = Path(cfg.workdir)
    else:
        workdir = Path(tempfile.mkdtemp(prefix=cfg.bench_prefix))
    cfg.run_data.workdir = workdir
    cfg.run_data.gitref = cfg.repo_branch

    os.chdir(str(workdir))

    bench_gitclone()

    os.chdir(str(workdir / 'bitcoin'))

    for commit in get_commits():
        if commit != 'HEAD':
            logger.info("Checking out commit %s", commit)
            _run("git checkout %s" % commit)

        cfg.run_data.gitref = commit
        cfg.run_data.gitsha = subprocess.check_output(
            shlex.split('git rev-parse HEAD')).strip().decode()

        for compiler in cfg.compilers:
            cfg.run_data.compiler = compiler
            bench_build()
            bench_makecheck()
            bench_functests()
            bench_microbench()

        connect_config = '-listen=0' if cfg.ibd_from_network else '-connect=0'
        addnode_config = (
            # If we aren't IBDing from random peers on the network, specify the
            # peer.
            ('-addnode=%s' % cfg.ibd_peer_address)
            if not cfg.ibd_from_network else '')

        run_bitcoind_cmd = (
            './src/bitcoind -datadir={}/bitcoin/data '
            '-dbcache={} -txindex=1 '
            '{} -debug=all -stopatheight={} -assumevalid={} '
            '-port={} -rpcport={} {} {}'.format(
                workdir, cfg.bitcoind_dbcache, connect_config,
                cfg.bitcoind_stopatheight, cfg.bitcoind_assumevalid,
                cfg.bitcoind_port, cfg.bitcoind_rpcport,
                addnode_config,
                BENCH_SPECIFIC_BITCOIND_ARGS,
            ))

        bench_ibd_and_reindex(run_bitcoind_cmd)


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
        _run("rm -rf %s" % cfg.run_data.workdir)
        logger.debug("shutdown: removed workdir at %s", cfg.run_data.workdir)
    elif cfg.no_teardown:
        logger.debug("shutdown: leaving workdir at %s", cfg.run_data.workdir)


def _stash_debug_file():
    # Move the debug.log file out into /tmp for diagnostics.
    debug_file = cfg.run_data.workdir / "/bitcoin/data/debug.log"
    if debug_file.is_file():
        # Overwrite the file so as not to fill up disk.
        debug_file.rename(Path("/tmp/bench-debug.log"))


def _run(*args, check_returncode=True, **kwargs) -> (bytes, bytes, int):
    p = subprocess.Popen(
        *args, **kwargs,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)

    (stdout, stderr) = p.communicate()

    if check_returncode and p.returncode != 0:
        raise RuntimeError(
            "Command '%s' failed with code %s\nstderr:\n%s\nstdout:\n%s" % (
                args[0], p.returncode, stderr, stdout))
    return (stdout, stderr, p.returncode)


def _popen(args, env=None):
    return subprocess.Popen(
        args, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)


def _try_execute_and_report(
        bench_name, cmd, *, report_memory=True, report_time=True, num_tries=1,
        check_returncode=True, executable='bitcoind'):
    """
    Attempt to execute some command a number of times and then report
    its execution memory usage or execution time to codespeed over HTTP.
    """
    for i in range(num_tries):
        start = time.time()
        ps = _popen('$(which time) -f %M ' + cmd)

        logger.info("[%s] command '%s' starting", bench_name, cmd)

        (stdout, stderr) = ps.communicate()
        total_time = time.time() - start
        # Get the last 10,000 characters of output.
        stdout = stdout.decode()[-10000:]
        stderr = stderr.decode()[-10000:]

        if (check_returncode and ps.returncode != 0) \
                or check_for_failure(
                    bench_name, stdout, stderr, total_time_secs=total_time):
            logger.error(
                "[%s] command failed\nstdout:\n%s\nstderr:\n%s",
                bench_name, stdout, stderr)

            if i == (num_tries - 1):
                return False
            continue
        else:
            # Command succeeded
            break

    memusage = int(stderr.strip().split('\n')[-1])

    logger.info(
        "[%s] command finished successfully "
        "with maximum resident set size %.3f MiB",
        bench_name, memusage / 1024)

    mem_name = bench_name + '.mem-usage'
    NAME_TO_TIME[cfg.run_data.gitref][mem_name].append(memusage)
    if report_memory:
        endpoints.send_to_codespeed(
            cfg,
            mem_name, memusage, executable, units_title='Size', units='KiB')

    logger.info(
        "[%s] command finished successfully in %.3f seconds (%s)",
        bench_name, total_time, datetime.timedelta(seconds=total_time))

    NAME_TO_TIME[cfg.run_data.gitref][bench_name].append(total_time)
    if report_time:
        endpoints.send_to_codespeed(
            cfg,
            bench_name, total_time, executable)


def check_for_failure(bench_name, stdout, stderr, total_time_secs):
    """
    Sometimes certain benchmarks may fail with zero returncodes and we must
    examine other things to detect the failure.
    """
    if re.match(r'ibd.*|reindex', bench_name):
        disk_warning_ps = subprocess.run(
            "tail -n 10000 %s/bitcoin/data/debug.log | "
            "grep 'Disk space is low!' " % cfg.run_data.workdir, shell=True)

        if disk_warning_ps.returncode == 0:
            logger.warning(
                "Ran out of disk space while running benchmark %s" %
                bench_name)
            return True

    if re.match(r'ibd.*', bench_name):
        one_hour_secs = 60 * 60 * 2

        if total_time_secs < one_hour_secs:
            logger.warning("IBD finished implausibly quickly")
            return True

    return False


def main():
    global cfg
    global logger
    cfg = config.parse_args()
    logger = cfg.logger

    atexit.register(_clean_shutdown)

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
