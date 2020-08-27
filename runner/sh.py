import subprocess
import logging
import time
import shutil
import os
import tempfile
import textwrap
import re
import typing as t
from pathlib import Path
from collections import namedtuple

from psutil import Process


logger = logging.getLogger('bitcoinperf')


def drop_caches(assert_drop: bool = False):
    ret = run("sync; sudo -n /sbin/swapoff -a;", check=False)

    if not ret.ok:
        # Don't log as harshly about this because disabling swap isn't as
        # important.
        logger.info(
            "!!! couldn't turn off swap. Bench results may be suspect! "
            "You probably need to tune your /etc/sudoers file.")
        if assert_drop:
            raise RuntimeError("failed to turn off swap")

    # N.B.: the host sudoer file needs to be configured to allow non-superusers
    # to run this command. See: https://unix.stackexchange.com/a/168670
    ret2 = run("sudo -n /sbin/sysctl vm.drop_caches=3", check=False)

    if not ret2.ok:
        logger.info(
            "!!! couldn't drop caches! Bench results may be suspect! "
            "You probably need to tune your /etc/sudoers file.")
        if assert_drop:
            raise RuntimeError("failed to drop caches")


def cd(*args, **kwargs):
    os.chdir(*args, **kwargs)
    logger.debug(f"chdir -> {args[0]}")


def rm(path: Path):
    logger.debug(f"rm {path}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


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

    Buffers output into tmpfiles to avoid blowing out memory. Allows easy
    reporting of runtime characteristics like time, memory usage, CPU usage,
    etc.
    """
    def __init__(self, cmd: str, bench_name: t.Optional[str] = None):
        """
        Args:
            bench_name: optional for logging context
        """
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
        prefix = f"[{self.bench_name}] " if self.bench_name else ""
        logger.debug(f"{prefix}command '%s' starting", self.cmd)

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
        assert self.start_time
        start = float(self.start_time)
        return (self.end_time or time.time()) - start

    @property
    def returncode(self) -> int:
        assert self.ps
        return self.ps.returncode

    def memusage_kib(self) -> int:
        if self.returncode is None:
            return self.get_resource_usage().rss_kb
        assert self.stderr
        return int(self.stderr.decode().strip().split('\n')[-1])

    def check_for_failure(self):
        """
        Parse output and returncode to determine if there was a failure.

        Sometimes certain benchmarks may fail with zero returncodes and we must
        check other things to detect the failure; e.g. disk space checks during
        IBD.
        """
        if self.returncode is None:
            raise RuntimeError("can't check for failure before completion")

        return self.returncode != 0

    def get_resource_usage(self) -> ResourceUsage:
        """
        Return various resource usage statistics about the running process.
        """
        assert not self.returncode, "Can't collect data on stopped process"
        assert self.ps

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


class RunReturn(namedtuple('RunReturn', 'args,returncode,stdout,stderr')):

    @property
    def ok(self):
        return self.returncode == 0

    @classmethod
    def from_std(cls, cp: subprocess.CompletedProcess):
        return cls(cp.args, cp.returncode, cp.stdout, cp.stderr)

    @property
    def output(self) -> str:
        return f"[stdout]\n\n{self.stdout}\n\n[stderr]\n\n{self.stderr}"

    def failure_msg(self, msg) -> str:
        return f"{msg}:\n{self.output}"


def run(cmd: str,
        check: bool = False,
        quiet: bool = False,
        **kwargs) -> RunReturn:
    """Run a command synchonrously."""
    kwargs.setdefault('text', True)
    kwargs.setdefault('shell', True)
    kwargs.setdefault('stdout', subprocess.PIPE)
    kwargs.setdefault('stderr', subprocess.PIPE)

    if quiet:
        kwargs['stdout'] = subprocess.DEVNULL
        kwargs['stderr'] = subprocess.DEVNULL

    logger.debug("Running cmd '%s': '%s'", cmd, kwargs)
    r = RunReturn.from_std(subprocess.run(cmd, **kwargs))

    if not r.ok:
        cmd_failed = (
            "Command failed (code {}): {}\n[ stdout ]\n{}\n[ stderr ]\n{}"
            .format(r.returncode, cmd, r.stdout, r.stderr))
        logger.debug(cmd_failed)

        if check:
            raise RuntimeError(cmd_failed)

    return r


CmdStrs = t.Union[str, t.Iterable[str]]


def runmany(cmds: CmdStrs, check: bool = True) -> t.List[RunReturn]:
    out = []

    for cmd in _split_cmd_input(cmds):
        r = run(cmd)
        out.append(r)

        if check and not r.ok:
            break

    return out


def _split_cmd_input(cmds: CmdStrs) -> t.List[str]:
    if isinstance(cmds, list):
        return cmds
    cmds = str(cmds)  # for mypy
    cmds = textwrap.dedent(cmds)
    # Eat linebreaks
    cmds = re.sub(r'\s+\\\n\s+', ' ', cmds)
    return [i.strip() for i in cmds.splitlines() if i]
