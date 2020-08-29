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
import typing as t
from pathlib import Path

from .util import md_table

import psutil

_SYS = platform.system()


def get_disk_iops(locations=None):
    """
    Use fio to get disk iops for a certain location, probably a datadir.
    """
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
            "--iodepth=64 --size=180M --time_based=1 --runtime=20 "
            "--readwrite=randrw --rwmixread=75")

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


def get_hwinfo(datadir_path: Path, srcdir_path: t.Optional[str]):
    paths_for_io = [Path(datadir_path or os.getcwd())]
    out_dict = dict(
        hostname=socket.gethostname(),
        cpu_model_name=get_processor_name(),
        cpu_count=psutil.cpu_count(),
        ram_gb=(psutil.virtual_memory().total / (1024**3)),
        os=list(distro.linux_distribution()),
        arch=platform.machine(),
        kernel=platform.uname().release,
        disk=get_disk_iops(paths_for_io),
    )

    if srcdir_path:
        out_dict.update(parse_configure_log(srcdir_path))

    return out_dict


def parse_configure_log(src_dir_path: t.Union[str, Path]) -> dict:
    """
    Inspect the config.log file from the bitcoin src dir.
    """
    out = {
        'configure_command': '',
        'clang_version': '',
        'gcc_version': '',
        'cxx': '',
        'cxxflags': '',
    }
    configlog = Path(Path(src_dir_path) / 'config.log')
    if not configlog.is_file():
        print("No config.log found at %s", configlog)

    lines = configlog.read_text().splitlines()

    def extract_val(line) -> str:
        return line.split('=', 1)[-1].replace("'", '')

    for line in lines:
        if line.startswith("  $") and 'configure ' in line:
            out['configure_command'] = line.strip('  $')

        elif line.startswith('clang version'):
            out['clang_version'] = line

        elif line.startswith('g++ '):
            out['gcc_version'] = line

        elif line.startswith('CXX='):
            out['cxx'] = extract_val(line)

        elif line.startswith('CXXFLAGS='):
            out['cxxflags'] += extract_val(line)

        elif '_CXXFLAGS=' in line:
            val = extract_val(line)
            if val:
                out['cxxflags'] += val + ' '

    for key in ('cxx', 'configure_command', 'cxxflags'):
        out[key] = '`' + out[key].strip() + '`'

    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', type=bool, default=False)
    parser.add_argument(
        '--datadir', type=str, default='',
        help='Specify the datadir location to give accurate disk IO measure.')
    parser.add_argument(
        '--srcdir', type=str, default='',
        help='Specify the source dir location to give compiler info.')
    args = parser.parse_args()

    get_hw_args = [args.datadir, args.srcdir]

    if args.json:
        print(json.dumps(get_hwinfo(*get_hw_args)))
    else:
        info = get_hwinfo(*get_hw_args)
        disk_info = info.pop('disk')
        print_list = []
        print_list.extend(list(info.items()))

        for disk, d in disk_info.items():
            for k, v in d.items():
                print_list.append((f'{k} ({disk})', v))

        print(md_table(('key', 'value'), print_list))

    sys.exit(0)


if __name__ == '__main__':
    main()
