import subprocess
import logging
import typing as t


logger = logging.getLogger('bitcoinperf')


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
