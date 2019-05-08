import subprocess
import logging
import time
import shutil
import typing as t
from pathlib import Path

from psutil import Process


logger = logging.getLogger('bitcoinperf')


def drop_caches():
    # N.B.: the host sudoer file needs to be configured to allow non-superusers
    # to run this command. See: https://unix.stackexchange.com/a/168670
    run("sudo /sbin/sysctl vm.drop_caches=3")


def rm(path: Path):
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def run(*args, check_returncode=True, **kwargs) -> (bytes, bytes, int):
    logger.debug("Running command %r", args)
    p = subprocess.Popen(
        *args, **kwargs,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)

    (stdout, stderr) = p.communicate()

    if check_returncode and p.returncode != 0:
        raise RuntimeError(
            "Command '%s' failed with code %s\nstderr:\n%s\nstdout:\n%s" % (
                args[0], p.returncode, stderr, stdout))
    return (stdout, stderr, p.returncode)


def popen(args, env=None):
    logger.debug("Running command %r", args)
    return subprocess.Popen(
        args, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)


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
        logger.debug("[%s] command '%s' starting", self.bench_name, self.cmd)

    def join(self):
        (self.stdout, self.stderr) = self.ps.communicate()
        self.end_time = time.time()

    @property
    def total_secs(self) -> float:
        return (self.end_time or time.time()) - self.start_time

    @property
    def returncode(self):
        return self.ps.returncode

    def memusage_kib(self) -> int:
        if self.returncode is None:
            return get_bitcoind_meminfo_kib(Process(self.ps.pid))
        return int(self.stderr.decode().strip().split('\n')[-1])

    def check_for_failure(self):
        """
        Parse output and returncode to determine if there was a failure.

        Sometimes certain benchmarks may fail with zero returncodes and we must
        check other things to detect the failure.
        """
        if self.returncode is None:
            raise RuntimeError("can't check for failure before completion")

        return self.returncode != 0


def get_bitcoind_meminfo_kib(ps: Process) -> int:
    """
    Process graph looks like this:

        sh(327)───time(334)───bitcoind(335)
    """
    # Recurse into child processes if need be.
    if ps.name() in ['sh', 'time']:
        assert len(ps.children()) == 1
        return get_bitcoind_meminfo_kib(ps.children()[0])

    assert ps.name().startswith('bitcoin')

    # First element of the `memory_info()` tuple is RSS in bytes.
    # See https://psutil.readthedocs.io/en/latest/#psutil.Process.memory_info
    return int(ps.memory_info()[0] / 1024)