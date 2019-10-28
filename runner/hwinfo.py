"""
Routines for gathering stuctured information on hardware being used by
bitcoinperf.
"""

import argparse
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
            "--iodepth=64 --size=180M --time_based=1 --runtime=20 --readwrite=randrw --rwmixread=75")

        res = subprocess.run(cmd.split(), capture_output=True)

        if res.returncode != 0:
            print("Fio command (`{}`) failed ({}): {}".format(
                ' '.join(res.args), res.returncode, res.stdout,
            ))

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


def get_hwinfo(datadir_path: str):
    paths_for_io = [Path('/tmp'), Path(datadir_path or '.')]

    return dict(
        hostname=socket.gethostname(),
        cpu_model_name=get_processor_name(),
        ram_gb=(psutil.virtual_memory().total / (1024**3)),
        os=list(distro.linux_distribution()),
        arch=platform.machine(),
        kernel=platform.uname().release,
        disk=get_disk_iops(paths_for_io),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', type=bool, default=False)
    parser.add_argument(
        '--datadir', type=str, default='',
        help='Specify the datadir location to give accurate disk IO measure.')
    args = parser.parse_args()

    if args.json:
        print(json.dumps(get_hwinfo(args.datadir)))
    else:
        info = get_hwinfo(args.datadir)
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
