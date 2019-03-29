import subprocess
import logging
import time
import typing as t


logger = logging.getLogger('bitcoinperf')


def drop_caches():
    # N.B.: the host sudoer file needs to be configured to allow non-superusers
    # to run this command. See: https://unix.stackexchange.com/a/168670
    run("sudo /sbin/sysctl vm.drop_caches=3")


def get_proc_peakmem_kib(
        ps: t.Union[int, subprocess.Popen]) -> t.Optional[int]:
    """
    Returns the peak memory usage in Kibibytes for a running process
    (according to /proc).

    We use /usr/bin/time to do this when we can wait for a process to
    terminate, but sometimes we need to take incremental measurements (e.g.
    IBD).

    """
    pid = ps.pid if hasattr(ps, 'pid') else ps
    ran = run("grep VmPeak /proc/{}/status".format(pid))

    if ran[-1] != 0:
        logger.error(
            "Unable to get peak mem usage for running process %s", ps)
        return None

    stdout = ran[0].decode().split()

    if stdout[-1] != 'kB':
        logger.error(
            "Expected mem size to be reported in kB, got %s (ps %s)",
            stdout[-1], ps)
        return None

    try:
        mem_kb = int(stdout[1])
    except ValueError:
        logger.error("Expected int for mem usage, got %r", stdout[1])
        return None

    # convert kB to KiB
    return int(mem_kb * 0.976562)


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
            return get_proc_peakmem_kib(self.ps)
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
