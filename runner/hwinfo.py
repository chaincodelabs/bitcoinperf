"""
Routines for gathering stuctured information on the hardware.
"""

import os
import platform
import subprocess
import json
import socket
import distro
import re
import sys
from pathlib import Path

import psutil

_SYS = platform.system()


def get_disk_iops(locations=None):
    """
    Use fio to get disk iops for a certain location, probably a datadir.
    """
    locations = locations or [Path('/tmp')]
    out = {}

    if subprocess.run('fio --version >/dev/null', shell=True).returncode != 0:
        raise RuntimeError("Must install fio (sudo apt install fio)")

    def get_iops(line):
        try:
            return re.search(r'iops=([^,]+)', line.lower()).group(1)
        except Exception:
            return ''

    for location in locations:
        out[str(location)] = {'read_iops': '', 'write_iops': ''}
        filename = location / 'random_read_write.fio'

        cmd = (
            "fio --randrepeat=1 --ioengine=libaio --direct=1 --gtod_reduce=1 "
            f"--name=test --filename={filename} --bs=4k "
            "--iodepth=64 --size=90M --readwrite=randrw --rwmixread=75")

        res = subprocess.run(cmd.split(), check=True, capture_output=True)

        for line in (i.decode().strip() for i in res.stdout.splitlines()):
            if line.startswith('read'):
                out[str(location)]['read_iops'] = get_iops(line)
            elif line.startswith('write'):
                out[str(location)]['write_iops'] = get_iops(line)

    return out


def get_processor_name():
    """
    Lifted from StackOverflow: https://stackoverflow.com/a/13078519
    """
    if _SYS == "Windows":
        return platform.processor()
    elif _SYS == "Darwin":
        os.environ['PATH'] += os.pathsep + '/usr/sbin'
        return subprocess.check_output(
            "sysctl -n machdep.cpu.brand_string").strip()
    elif _SYS == "Linux":
        for line in open('/proc/cpuinfo', 'r').readlines():
            if "model name" in line:
                return line.split(':', 1)[-1].strip()
    return ""


def get_hwinfo():
    return dict(
        hostname=socket.gethostname(),
        cpu_model_name=get_processor_name(),
        ram_gb=(psutil.virtual_memory().total / (1024**3)),
        os=list(distro.linux_distribution()),
        arch=platform.machine(),
        kernel=platform.uname().release,
        disk=get_disk_iops([Path('/tmp'), Path('.')]),
    )


def main():
    argv = set(sys.argv)

    if argv & {'--help', '-h'}:
        print("Usage: bitcoinperf-hwinfo [--json]")
    elif argv & {'--json'}:
        print(json.dumps(get_hwinfo()))
    else:
        info = get_hwinfo()
        disk_info = info.pop('disk')

        for k, v in info.items():
            print(f"{k:<22} {str(v):<20}")

        for disk, d in disk_info.items():
            for k, v in d.items():
                name = f'{k} ({disk})'
                print(f"{name:<22} {v}")

    sys.exit(0)


if __name__ == '__main__':
    main()
