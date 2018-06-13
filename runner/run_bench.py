#!/usr/bin/env python3.6
"""
Run a series of benchmarks against a particular Bitcoin Core revision.

See bin/run_bench for a sample invocation.
"""

import atexit
import os
import subprocess
import json
import datetime
import contextlib
import time
import requests
import logging
import shlex
import sys
import getpass
import typing as t
from collections import defaultdict
from pathlib import Path


class SlackLogHandler(logging.Handler):
    def emit(self, record):
        return send_to_slack(self.format(record))


def _get_logger():
    logger = logging.getLogger(__name__)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(os.environ.get('LOG_LEVEL', 'DEBUG'))
    sh.setFormatter(logging.Formatter(
        '%(asctime)s %(name)s [%(levelname)s] %(message)s'))

    slack = SlackLogHandler()
    slack.setLevel('WARNING')
    slack.setFormatter(logging.Formatter('%(message)s'))

    logger.addHandler(sh)
    logger.addHandler(slack)
    logger.setLevel('DEBUG')
    return logger


logger = _get_logger()

REPO_LOCATION = os.environ.get(
    'REPO_LOCATION', 'https://github.com/bitcoin/bitcoin.git')
REPO_BRANCH = os.environ.get('REPO_BRANCH', 'master')
CODESPEED_URL = os.environ.get('CODESPEED_URL', 'http://localhost:8000')
IBD_PEER_ADDRESS = os.environ.get('IBD_PEER_ADDRESS', '')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
CODESPEED_USER = os.environ['CODESPEED_USER']
CODESPEED_PASSWORD = os.environ['CODESPEED_PASSWORD']
CODESPEED_ENV_NAME = os.environ['CODESPEED_ENV_NAME']
SKIP_BUILD = bool(os.environ.get('SKIP_BUILD', ''))
BENCHES_TO_RUN = [
    i for i in os.environ.get('BENCHES_TO_RUN', '').split(',') if i]
CHECKOUT_COMMIT = os.environ.get('CHECKOUT_COMMIT')

BITCOIND_DBCACHE = os.environ.get('BITCOIND_DBCACHE', '2048')
BITCOIND_STOPATHEIGHT = os.environ.get('BITCOIND_STOPATHEIGHT', '522000')
BITCOIND_PORT = os.environ.get('BITCOIND_PORT', '9003')
BITCOIND_RPCPORT = os.environ.get('BITCOIND_RPCPORT', '9004')

WORKING_DIR_NAME = (
    f"/tmp/bench-{REPO_BRANCH}-"
    f"{datetime.datetime.utcnow().strftime('%Y-%m-%d')}")

# FIXME reenable this at some point
# NPROC = int(multiprocessing.cpu_count())
NPROC = 4
NPROC = int(os.environ.get('NPROC', str(NPROC)))
CODESPEED_NO_SEND = bool(os.environ.get('CODESPEED_NO_SEND', ''))


NAME_TO_TIME: t.Dict[str, int] = defaultdict(list)


@contextlib.contextmanager
def timer(name: str):
    start = time.time()
    yield
    NAME_TO_TIME[name].append(time.time() - start)


class RunData:
    current_commit: str = None

    # The working directory for this benchmark. Contains a `bitcoin/` subdir.
    workdir: Path = None

    # Did we acquire the benchmarking lockfile?
    lockfile_acquired: bool = False


RUN_DATA = RunData()

# Maintain a lockfile that is global across the host to ensure that we're not
# running more than one instance on a given system.
LOCKFILE_PATH = Path("/tmp/bitcoin_bench.lock")


def run_benches():
    """
    Create a tmp directory in which we will clone bitcoin, build it, and run
    various benchmarks.
    """
    if not _try_acquire_lockfile():
        logger.error(f"Couldn't acquire lockfile {LOCKFILE_PATH}; exiting")
        sys.exit(1)

    workdir: Path = _create_working_dir()
    RUN_DATA.workdir = workdir

    os.chdir(workdir)

    if _shouldrun('gitclone'):
        with timer("gitclone"):
            _run(f"rm -rf {workdir / 'bitcoin'}")
            _run(f"git clone -b {REPO_BRANCH} {REPO_LOCATION}")

    os.chdir(workdir / 'bitcoin')

    if CHECKOUT_COMMIT:
        _run(f"git checkout {CHECKOUT_COMMIT}")

    RUN_DATA.current_commit = subprocess.check_output(
        shlex.split('git rev-parse HEAD')).strip()
    send_to_slack(
        f"Starting benchmark for {REPO_BRANCH} "
        f"({str(RUN_DATA.current_commit)})")

    if _shouldrun('build'):
        _run(f"./contrib/install_db4.sh .")

        my_env = os.environ.copy()
        my_env['BDB_PREFIX'] = f"{workdir}/bitcoin/db4"

        _run(f"./autogen.sh")
        _run(
            './configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" '
            'BDB_CFLAGS="-I${BDB_PREFIX}/include" '
            # Ensure ccache is disabled so that subsequent make runs are
            # timed accurately.
            '--disable-ccache',
            env=my_env)
        _try_execute_and_report(
            f'build.make.1', f"make -j 1",
            executable='make')

    if _shouldrun('makecheck'):
        _try_execute_and_report(
            f'makecheck.{NPROC - 1}', f"make -j {NPROC - 1} check",
            num_tries=3, executable='make')

    if _shouldrun('functionaltests'):
        _try_execute_and_report(
            'functionaltests', f"./test/functional/test_runner.py",
            num_tries=3, executable='functional-test-runner')

    if _shouldrun('microbench'):
        with timer("microbench"):
            microbench_ps = _popen("./src/bench/bench_bitcoin")
            (microbench_output, _) = microbench_ps.communicate()

        microbench_lines = [
            # Skip the first line (header)
            i.decode().split(', ') for i in microbench_output.splitlines()[1:]]

        for line in microbench_lines:
            # Line strucure is
            # "Benchmark, evals, iterations, total, min, max, median"
            assert(len(line) == 7)
            (bench, median, max_, min_) = (
                line[0], line[-1], line[-2], line[-3])
            if not (max_ >= median >= min_):
                logger.warning(
                    f"{bench} has weird results: {max_}, {median}, {min_}")
            send_to_codespeed(
                f"micro.{bench}",
                median, max_, min_, executable='bench-bitcoin')

    datadir = workdir / 'bitcoin' / 'data'
    _run(f"rm -rf {datadir}", check_returncode=False)
    os.mkdir(datadir)

    run_bitcoind_cmd = (
        f'./src/bitcoind -datadir={workdir}/bitcoin/data '
        f'-dbcache={BITCOIND_DBCACHE} -txindex=1 '
        f'-connect=0 -debug=all -stopatheight={BITCOIND_STOPATHEIGHT} '
        f'-port={BITCOIND_PORT} -rpcport={BITCOIND_RPCPORT}')

    if _shouldrun('ibd'):
        send_to_slack(
            f"Starting IBD for {REPO_BRANCH} ({RUN_DATA.current_commit})")

        _try_execute_and_report(
            f'ibd.{BITCOIND_STOPATHEIGHT}.dbcache={BITCOIND_DBCACHE}',
            f'{run_bitcoind_cmd} -addnode={IBD_PEER_ADDRESS}')

        send_to_slack(
            f"Finished IBD ({RUN_DATA.current_commit})")

    if _shouldrun('reindex'):
        send_to_slack(
            f"Starting reindex for {REPO_BRANCH} ({RUN_DATA.current_commit})")

        _try_execute_and_report(
            f'reindex.{BITCOIND_STOPATHEIGHT}.dbcache={BITCOIND_DBCACHE}',
            f'{run_bitcoind_cmd} -reindex')

        send_to_slack(
            f"Finished reindex ({RUN_DATA.current_commit})")


def _try_acquire_lockfile():
    if LOCKFILE_PATH.exists():
        return False

    with LOCKFILE_PATH.open('w') as f:
        f.write(f"{datetime.datetime.utcnow()},{getpass.getuser()}")
    RUN_DATA.lockfile_acquired = True
    return True


def _clean_shutdown():
    # Release lockfile if we've got it
    if RUN_DATA.lockfile_acquired:
        LOCKFILE_PATH.unlink()
        logger.debug("shutdown: removed lockfile at %s", LOCKFILE_PATH)

    # Clean up to avoid filling disk
    if RUN_DATA.workdir:
        os.chdir(RUN_DATA.workdir / "..")
        _run(f"rm -rf {RUN_DATA.workdir}")
        logger.debug("shutdown: removed workdir at %s", RUN_DATA.workdir)


atexit.register(_clean_shutdown)


def _create_working_dir():
    if not os.path.exists(WORKING_DIR_NAME):
        os.mkdir(WORKING_DIR_NAME)
    return Path(WORKING_DIR_NAME)


def _run(*args, check_returncode=True, **kwargs) -> (bytes, bytes, int):
    p = subprocess.Popen(
        *args, **kwargs,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)

    (stdout, stderr) = p.communicate()

    if check_returncode and p.returncode != 0:
        raise RuntimeError(
            f"Command '{args[0]}' failed with code {p.returncode}\n"
            f"stderr:\n{stderr}\nstdout:\n{stdout}")
    return (stdout, stderr, p.returncode)


def _popen(args, env=None):
    return subprocess.Popen(
        args, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)


def _shouldrun(bench_name):
    should = (not BENCHES_TO_RUN) or bench_name in BENCHES_TO_RUN

    if should:
        logger.info(f"Running benchmark '{bench_name}'")

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
        send_to_codespeed(mem_name, memusage, executable=executable)

    logger.info(
        "[%s] command '%s' finished successfully in %.3f seconds (%s)",
        bench_name, cmd, total_time, datetime.timedelta(seconds=total_time))

    NAME_TO_TIME[bench_name].append(total_time)
    if report_time:
        send_to_codespeed(bench_name, total_time, executable=executable)


def check_for_failure(bench_name, stdout, stderr, total_time_secs):
    """
    Sometimes certain benchmarks may fail with zero returncodes and we must
    examine other things to detect the failure.
    """
    if bench_name in ('ibd', 'reindex'):
        disk_warning_ps = subprocess.run(
            f"tail -n 10000 {WORKING_DIR_NAME}/bitcoin/data/debug.log | "
            "grep 'Disk space is low!'")

        if disk_warning_ps.returncode == 0:
            logger.warning(
                f"Ran out of disk space while running benchmark {bench_name}")
            return True

    if bench_name == 'ibd':
        one_hour_secs = 60 * 60 * 2

        if total_time_secs < one_hour_secs:
            logger.warning(f"IBD finished implausibly quickly")
            return True

    return False


def send_to_codespeed(bench_name, result,
                      result_max=None, result_min=None, executable='bitcoind'):
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


def send_to_slack(txt):
    if not SLACK_WEBHOOK_URL:
        return

    slack_data = {'text': txt}

    response = requests.post(
        SLACK_WEBHOOK_URL, data=json.dumps(slack_data),
        headers={'Content-Type': 'application/json'}
    )
    if response.status_code != 200:
        raise ValueError(
            'Request to slack returned an error %s, the response is:\n%s'
            % (response.status_code, response.text)
        )


def print_times_table():
    times = "\n"
    for name, times in NAME_TO_TIME.items():
        for i, time_ in enumerate(times):
            times += (
                f"{name:40} "
                f"{str(datetime.timedelta(seconds=time_)):<20}\n")

    print(times)
    send_to_slack(times)


if __name__ == '__main__':
    run_benches()
    print_times_table()
