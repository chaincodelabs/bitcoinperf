import subprocess


def run(*args, check_returncode=True, **kwargs) -> (bytes, bytes, int):
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
    return subprocess.Popen(
        args, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
