import subprocess
import logging
import time
import shutil
import os
import tempfile
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


def popen(args, env=None, stdout=None, stderr=None):
    logger.debug("Running command %r", args)
    return subprocess.Popen(
        args, env=env,
        stdout=(stdout or subprocess.PIPE),
        stderr=(stderr or subprocess.PIPE), shell=True)


class ResourceUsage(t.NamedTuple):
    # See https://psutil.readthedocs.io/en/latest/#psutil.Process.cpu_percent
    cpu_percent: float

    # See https://psutil.readthedocs.io/en/latest/#psutil.Process.memory_info
    memory_info: tuple

    # The number of file descriptors currently opened by this process.
    num_fds: int

    @property
    def rss_kb(self) -> int:
        return int(self.memory_info[0] / 1024)


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
        (self.stdout_fd, self.stdout_path) = tempfile.mkstemp(
            prefix='bitcoinperf-stdout-')
        (self.stderr_fd, self.stderr_path) = tempfile.mkstemp(
            prefix='bitcoinperf-stderr-')

    def start(self):
        self.start_time = time.time()
        self.ps = popen(
            '$(which time) -f %M ' + self.cmd,
            stdout=self.stdout_fd,
            stderr=self.stderr_fd,
        )
        logger.debug("[%s] command '%s' starting", self.bench_name, self.cmd)

    def join(self, timeout=None):
        self.ps.wait(timeout=timeout)
        self.end_time = time.time()
        self._read_outputs()

    def _read_outputs(self):
        self.stdout = Path(self.stdout_path).read_bytes()
        self.stderr = Path(self.stderr_path).read_bytes()
        os.unlink(self.stdout_path)
        os.unlink(self.stderr_path)

    @property
    def total_secs(self) -> float:
        return (self.end_time or time.time()) - self.start_time

    @property
    def returncode(self):
        return self.ps.returncode

    def memusage_kib(self) -> int:
        if self.returncode is None:
            return self.get_resource_usage().rss_kb
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

    def get_resource_usage(self) -> ResourceUsage:
        """
        Return various resource usage statistics about the running process.
        """
        assert not self.returncode, "Can't collect data on stopped process"

        proc = Process(self.ps.pid)

        if 'bitcoind' in self.cmd:
            def find_process(proc_):
                """
                Process graph looks like this:

                    sh(327)───time(334)───bitcoind(335)
                """
                name = proc_.name()

                # Recurse into child processes if need be.
                if name in ['sh', 'time']:
                    assert len(proc_.children()) == 1
                    return find_process(proc_.children()[0])

                assert (name.startswith('bitcoin') or name.startswith('b-'))
                return proc_

            proc = find_process(proc)

        with proc.oneshot():
            return ResourceUsage(
                cpu_percent=proc.cpu_percent(),
                memory_info=proc.memory_info(),
                num_fds=proc.num_fds(),
            )
