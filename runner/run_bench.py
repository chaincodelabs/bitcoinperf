#!/usr/bin/env python3
"""
Run a series of benchmarks against a particular Bitcoin Core revision.

See bin/run_bench for a sample invocation.

To run doctests:

    ./runner/run_bench.py test

"""

import atexit
import os
import subprocess
import tempfile
import json
import datetime
import contextlib
import time
import requests
import logging
import shlex
import socket
import sys
import getpass
import multiprocessing
import traceback
from collections import defaultdict
from pathlib import Path


REPO_LOCATION = os.environ.get(
    'REPO_LOCATION', 'https://github.com/bitcoin/bitcoin.git')
REPO_BRANCH = os.environ.get('REPO_BRANCH', 'master')

# Optional specification for where the temporary bitcoin clone will live.
WORKDIR = os.environ.get('WORKDIR', '')
IBD_PEER_ADDRESS = os.environ.get('IBD_PEER_ADDRESS', '')

# When using a local IBD peer, specify a datadir which contains a chain high
# enough to do the requested IBD.
SYNCED_DATA_DIR = os.environ.get('SYNCED_DATA_DIR', '')

if not IBD_PEER_ADDRESS and not SYNCED_DATA_DIR:
    raise RuntimeError(
        "must specify SYNCED_DATA_DIR when using a local peer for ibd")

CODESPEED_URL = os.environ.get('CODESPEED_URL', 'http://localhost:8000')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
BENCHES_TO_RUN = [
    i for i in os.environ.get('BENCHES_TO_RUN', '').split(',') if i]
CHECKOUT_COMMIT = os.environ.get('CHECKOUT_COMMIT')

BITCOIND_DBCACHE = os.environ.get('BITCOIND_DBCACHE', '2048')
BITCOIND_STOPATHEIGHT = os.environ.get('BITCOIND_STOPATHEIGHT', '522000')
BITCOIND_PORT = os.environ.get('BITCOIND_PORT', '9003')
BITCOIND_RPCPORT = os.environ.get('BITCOIND_RPCPORT', '9004')

# Where the bitcoind binary which will serve blocks for IBD lives.
SYNCED_BITCOIN_REPO_DIR = os.environ.get(
    'SYNCED_BITCOIN_REPO_DIR', os.environ['HOME'] + '/bitcoin')

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG')

NPROC = min(4, int(multiprocessing.cpu_count()))
NPROC = int(os.environ.get('NPROC', str(NPROC)))

# If true, leave the Bitcoin checkout intact after finishing
NO_TEARDOWN = bool(os.environ.get('NO_TEARDOWN', ''))

# If true, don't perform a variety of startup checks and cache drops
NO_CAUTION = bool(os.environ.get('NO_CAUTION', ''))

HOSTNAME = socket.gethostname()

CODESPEED_NO_SEND = bool(os.environ.get('CODESPEED_NO_SEND', ''))
CODESPEED_USER = os.environ.get('CODESPEED_USER')
CODESPEED_PASSWORD = os.environ.get('CODESPEED_PASSWORD')
# Prefill a sensisble default if we recognize the hostname
CODESPEED_ENV_NAME = {
    'bench-odroid-1': 'ccl-bench-odroid-1',
    'bench-raspi-1': 'ccl-bench-raspi-1',
    'bench-hdd-1': 'ccl-bench-hdd-1',
    'bench-ssd-1': 'ccl-bench-ssd-1',
}.get(HOSTNAME, os.environ.get('CODESPEED_ENV_NAME'))


if not CODESPEED_NO_SEND:
    assert(CODESPEED_USER)
    assert(CODESPEED_PASSWORD)
    assert(CODESPEED_ENV_NAME)


class SlackLogHandler(logging.Handler):
    def emit(self, record):
        return send_to_slack_txt(self.format(record))


def _get_logger():
    logger = logging.getLogger(__name__)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(LOG_LEVEL)
    sh.setFormatter(logging.Formatter(
        '%(asctime)s %(name)s [%(levelname)s] %(message)s'))

    slack = SlackLogHandler()
    slack.setLevel('WARNING')
    slack.setFormatter(logging.Formatter('%(message)s'))

    logger.addHandler(sh)
    logger.addHandler(slack)
    logger.setLevel(LOG_LEVEL)
    return logger


logger = _get_logger()

RUNNING_SYNCED_BITCOIND_LOCALLY = False

if not IBD_PEER_ADDRESS:
    RUNNING_SYNCED_BITCOIND_LOCALLY = True
    IBD_PEER_ADDRESS = '127.0.0.1'
    logger.info(
        "Running synced chain node on localhost (no remote addr specified)")


class RunData:
    current_commit = None

    # The working directory for this benchmark. Contains a `bitcoin/` subdir.
    workdir = None

    # Did we acquire the benchmarking lockfile?
    lockfile_acquired = False


RUN_DATA = RunData()

NAME_TO_TIME = defaultdict(list)


@contextlib.contextmanager
def timer(name: str):
    start = time.time()
    yield
    NAME_TO_TIME[name].append(time.time() - start)


# Maintain a lockfile that is global across the host to ensure that we're not
# running more than one instance on a given system.
LOCKFILE_PATH = Path("/tmp/bitcoin_bench.lock")


@contextlib.contextmanager
def run_synced_bitcoind():
    """
    Context manager which spawns (and cleans up) a bitcoind instance that has a
    synced chain high enough to service an IBD up to BITCOIND_STOPATHEIGHT.
    """
    if not RUNNING_SYNCED_BITCOIND_LOCALLY:
        # If we're not running a node locally, don't worry about setup and
        # teardown.
        yield
        return

    bitcoinps = _popen(
        # Relies on bitcoind being precompiled and synced chain data existing
        # in /bitcoin_data; see runner/Dockerfile.
        "%s/src/bitcoind -datadir=%s "
        "-rpcuser=foo -rpcpassword=bar -noconnect -listen=1 "
        "-maxtipage=99999999999999" % (
            SYNCED_BITCOIN_REPO_DIR, SYNCED_DATA_DIR))

    logger.info(
        "started synced node with '%s' (pid %s)",
        bitcoinps.args, bitcoinps.pid)

    # Wait for bitcoind to come up.
    num_tries = 100
    sleep_time_secs = 2
    bitcoind_up = False

    while num_tries > 0 and bitcoinps.returncode is None and not bitcoind_up:
        info = None
        info_call = _run(
            "%s/src/bitcoin-cli -rpcuser=foo -rpcpassword=bar "
            "getblockchaininfo" % SYNCED_BITCOIN_REPO_DIR,
            check_returncode=False)

        if info_call[2] == 0:
            info = json.loads(info_call[0].decode())
        else:
            logger.debug(
                "non-zero returncode (%s) from synced bitcoind status check",
                info_call[2])

        if info and info["blocks"] < int(BITCOIND_STOPATHEIGHT):
            raise RuntimeError(
                "synced bitcoind node doesn't have enough blocks "
                "(%s vs. %s)" % (info['blocks'], int(BITCOIND_STOPATHEIGHT)))
        elif info:
            bitcoind_up = True
        else:
            num_tries -= 1
            time.sleep(sleep_time_secs)

    if not bitcoind_up:
        raise RuntimeError("Couldn't bring synced node up")

    logger.info("synced node is active (pid %s)", bitcoinps.pid)

    try:
        yield
    finally:
        logger.info("shutting down synced node (pid %s)", bitcoinps.pid)
        _run(
            "%s/src/bitcoin-cli -rpcuser=foo -rpcpassword=bar stop" %
            SYNCED_BITCOIN_REPO_DIR)
        bitcoinps.wait(timeout=120)

        if bitcoinps.returncode != 0:
            logger.warning(
                "synced bitcoind returned with nonzero return code "
                "%s" % bitcoinps.returncode)


def _drop_caches():
    # N.B.: the host sudoer file needs to be configured to allow non-superusers
    # to run this command. See: https://unix.stackexchange.com/a/168670
    if not NO_CAUTION:
        _run("sudo /sbin/sysctl vm.drop_caches=3")


def _startup_assertions():
    """
    Ensure the benchmark environment is suitable in various ways.
    """
    if _run("pgrep bitcoin", check_returncode=False)[2] == 0 and \
            not NO_CAUTION:
        raise RuntimeError(
            "benchmarks shouldn't run concurrently with unrelated bitcoin "
            "processes")

    if _run('cat /proc/swaps | grep -v "^Filename"',
            check_returncode=False)[2] != 1 and not NO_CAUTION:
        raise RuntimeError(
            "swap must be disabled during benchmarking")

    if not _try_acquire_lockfile():
        raise RuntimeError(
            "Couldn't acquire lockfile %s; exiting", LOCKFILE_PATH)


def run_benches():
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    _startup_assertions()

    if WORKDIR:
        workdir = Path(WORKDIR)
    else:
        workdir = Path(tempfile.mkdtemp(prefix=(
            "bench-%s-%s-" %
            (REPO_BRANCH, datetime.datetime.utcnow().strftime('%Y-%m-%d')))))
    RUN_DATA.workdir = workdir

    os.chdir(str(workdir))

    if _shouldrun('gitclone'):
        _drop_caches()
        with timer("gitclone"):
            _run("git clone -b %s %s" % (REPO_BRANCH, REPO_LOCATION))

    os.chdir(str(workdir / 'bitcoin'))

    if CHECKOUT_COMMIT:
        _run("git checkout %s" % CHECKOUT_COMMIT)

    RUN_DATA.current_commit = subprocess.check_output(
        shlex.split('git rev-parse HEAD')).strip().decode()
    send_to_slack_attachment("Starting benchmark", {})

    for compiler in ('clang', 'gcc'):
        if _shouldrun('build'):
            _run("./contrib/install_db4.sh .")

            my_env = os.environ.copy()
            my_env['BDB_PREFIX'] = "%s/bitcoin/db4" % workdir

            _run("./autogen.sh")

            configure_prefix = ''
            if compiler == 'clang':
                configure_prefix = 'CC=clang CXX=clang++ '
            else:
                _run('make distclean')  # Clean after clang run

            _run(
                configure_prefix +
                './configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" '
                'BDB_CFLAGS="-I${BDB_PREFIX}/include" '
                # Ensure ccache is disabled so that subsequent make runs are
                # timed accurately.
                '--disable-ccache',
                env=my_env)

            _drop_caches()
            _try_execute_and_report(
                'build.make.1.%s' % compiler, "make -j 1",
                executable='make')

        if _shouldrun('makecheck'):
            _drop_caches()
            _try_execute_and_report(
                'makecheck.%s.%s' % (compiler, NPROC - 1),
                "make -j %s check" % (NPROC - 1),
                num_tries=3, executable='make')

        if _shouldrun('functionaltests'):
            _drop_caches()
            _try_execute_and_report(
                'functionaltests.%s' % compiler,
                "./test/functional/test_runner.py",
                num_tries=3, executable='functional-test-runner')

        if _shouldrun('microbench'):
            with timer("microbench.%s" % compiler):
                _drop_caches()
                microbench_ps = _popen("./src/bench/bench_bitcoin")
                (microbench_output, _) = microbench_ps.communicate()

            microbench_lines = [
                # Skip the first line (header)
                i.decode().split(', ')
                for i in microbench_output.splitlines()[1:]]

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
                send_to_codespeed(
                    "micro.%s.%s" % (compiler, bench),
                    median, 'bench-bitcoin', result_max=max_, result_min=min_)

    datadir = workdir / 'bitcoin' / 'data'
    _run("rm -rf %s" % datadir, check_returncode=False)
    if not datadir.exists():
        datadir.mkdir()

    run_bitcoind_cmd = (
        './src/bitcoind -datadir=%s/bitcoin/data '
        '-dbcache=%s -txindex=1 '
        '-connect=0 -debug=all -stopatheight=%s '
        '-port=%s -rpcport=%s' % (
            workdir, BITCOIND_DBCACHE, BITCOIND_STOPATHEIGHT,
            BITCOIND_PORT, BITCOIND_RPCPORT
        ))

    if _shouldrun('ibd'):
        with run_synced_bitcoind():
            _drop_caches()
            _try_execute_and_report(
                'ibd.%s.dbcache=%s' % (
                    BITCOIND_STOPATHEIGHT, BITCOIND_DBCACHE),
                '%s -addnode=%s' % (
                    run_bitcoind_cmd, IBD_PEER_ADDRESS),
                )

    if _shouldrun('reindex'):
        _drop_caches()
        _try_execute_and_report(
            'reindex.%s.dbcache=%s' % (
                BITCOIND_STOPATHEIGHT, BITCOIND_DBCACHE),
            '%s -reindex' % run_bitcoind_cmd)


def _try_acquire_lockfile():
    if LOCKFILE_PATH.exists():
        return False

    with LOCKFILE_PATH.open('w') as f:
        f.write("%s,%s" % (datetime.datetime.utcnow(), getpass.getuser()))
    RUN_DATA.lockfile_acquired = True
    return True


def _clean_shutdown():
    # Release lockfile if we've got it
    if RUN_DATA.lockfile_acquired:
        LOCKFILE_PATH.unlink()
        logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

    # Clean up to avoid filling disk
    if RUN_DATA.workdir and not NO_TEARDOWN:
        os.chdir(str(RUN_DATA.workdir / ".."))
        _run("rm -rf %s" % RUN_DATA.workdir)
        logger.debug("shutdown: removed workdir at %s", RUN_DATA.workdir)
    elif NO_TEARDOWN:
        logger.debug("shutdown: leaving workdir at %s", RUN_DATA.workdir)


atexit.register(_clean_shutdown)


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


def _shouldrun(bench_name):
    should = (not BENCHES_TO_RUN) or bench_name in BENCHES_TO_RUN

    if should:
        logger.info("Running benchmark '%s'" % bench_name)

    return should


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
        stdout = stdout.decode()[:100000]
        stderr = stderr.decode()[:100000]

        if (check_returncode and ps.returncode != 0) \
                or check_for_failure(
                    bench_name, stdout, stderr, total_time_secs=total_time):
            logger.error(
                "[%s] command '%s' failed\nstdout:\n%s\nstderr:\n%s",
                bench_name, cmd, stdout, stderr)

            if i == (num_tries - 1):
                return False
            continue
        else:
            # Command succeeded
            break

    memusage = int(stderr.strip().split('\n')[-1])

    logger.info(
        "[%s] command '%s' finished successfully "
        "with maximum resident set size %.3f MiB",
        bench_name, cmd, memusage / 1024)

    mem_name = bench_name + '.mem-usage'
    NAME_TO_TIME[mem_name].append(memusage)
    if report_memory:
        send_to_codespeed(
            mem_name, memusage, executable, units_title='Size', units='KiB')

    logger.info(
        "[%s] command '%s' finished successfully in %.3f seconds (%s)",
        bench_name, cmd, total_time, datetime.timedelta(seconds=total_time))

    NAME_TO_TIME[bench_name].append(total_time)
    if report_time:
        send_to_codespeed(bench_name, total_time, executable)


def check_for_failure(bench_name, stdout, stderr, total_time_secs):
    """
    Sometimes certain benchmarks may fail with zero returncodes and we must
    examine other things to detect the failure.
    """
    if bench_name in ('ibd', 'reindex'):
        disk_warning_ps = subprocess.run(
            "tail -n 10000 %s/bitcoin/data/debug.log | "
            "grep 'Disk space is low!' " % RUN_DATA.workdir)

        if disk_warning_ps.returncode == 0:
            logger.warning(
                "Ran out of disk space while running benchmark %s" %
                bench_name)
            return True

    if bench_name == 'ibd':
        one_hour_secs = 60 * 60 * 2

        if total_time_secs < one_hour_secs:
            logger.warning("IBD finished implausibly quickly")
            return True

    return False


def send_to_codespeed(
        bench_name, result, executable,
        lessisbetter=True, units_title='Time', units='seconds', description='',
        result_max=None, result_min=None):
    """
    Send a benchmark result to codespeed over HTTP.
    """
    # Mandatory fields
    data = {
        'commitid': RUN_DATA.current_commit,
        'branch': REPO_BRANCH,
        'project': 'Bitcoin Core',
        'executable': executable,
        'benchmark': bench_name,
        'environment': CODESPEED_ENV_NAME,
        'result_value': result,
        # Optional. Default is taken either from VCS integration or from
        # current date
        # 'revision_date': current_date,
        # 'result_date': current_date,  # Optional, default is current date
        # 'std_dev': std_dev,  # Optional. Default is blank
        'max': result_max,  # Optional. Default is blank
        'min': result_min,  # Optional. Default is blank
        # Ignored if bench_name already exists:
        'lessisbetter': lessisbetter,
        'units_title': units_title,
        'units': units,
        'description': description,
    }

    logger.debug(
        "Attempting to send benchmark (%s, %s) to codespeed",
        bench_name, result)

    if CODESPEED_NO_SEND:
        return

    resp = requests.post(
        CODESPEED_URL + '/result/add/',
        data=data, auth=(CODESPEED_USER, CODESPEED_PASSWORD))

    if resp.status_code != 202:
        raise ValueError(
            'Request to codespeed returned an error %s, the response is:\n%s'
            % (resp.status_code, resp.text)
        )


def send_to_slack_txt(txt):
    _send_to_slack({'text': "[%s] %s" % (HOSTNAME, txt)})


def send_to_slack_attachment(title, fields, text="", success=True):
    fields['Host'] = HOSTNAME
    fields['Commit'] = RUN_DATA.current_commit[:6]
    fields['Branch'] = REPO_BRANCH

    data = {
        "attachments": [{
            "title": title,
            "fields": [
                {"title": title, "value": val, "short": True} for (title, val)
                in fields.items()
            ],
            "color": "good" if success else "danger",
        }],
    }

    if text:
        data['attachments'][0]['text'] = text

    _send_to_slack(data)


def _send_to_slack(slack_data):
    if not SLACK_WEBHOOK_URL:
        return

    response = requests.post(
        SLACK_WEBHOOK_URL, data=json.dumps(slack_data),
        headers={'Content-Type': 'application/json'}
    )
    if response.status_code != 200:
        raise ValueError(
            'Request to slack returned an error %s, the response is:\n%s'
            % (response.status_code, response.text)
        )


def get_times_table(name_to_times_map):
    """
    >>> print(get_times_table(
    ...    {'a': [1, 2, 3], 'foo': [2.3], 'b.mem-usage': [3000]}))
    <BLANKLINE>
    a: 0:00:01
    a: 0:00:02
    a: 0:00:03
    b.mem-usage: 3.0MiB
    foo: 0:00:02.300000
    <BLANKLINE>

    """
    timestr = "\n"
    for name, times in sorted(name_to_times_map.items()):
        for time_ in times:
            val = str(datetime.timedelta(seconds=float(time_)))

            if 'mem-usage' in name:
                val = "%sMiB" % (int(time_) / 1000.)

            timestr += "{0}: {1}\n".format(name, val)

    return timestr


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        import doctest
        doctest.testmod()
    else:
        try:
            run_benches()
            timestr = get_times_table(NAME_TO_TIME)
            print(timestr)
            send_to_slack_attachment("Benchmark complete", {}, text=timestr)
        except Exception:
            send_to_slack_attachment(
                "Error", {}, text=traceback.format_exc(), success=False)
