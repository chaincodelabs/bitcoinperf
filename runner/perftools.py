"""
Tools for using `perf`, the linux profiler.

"""

import argparse
import subprocess
import logging
from pathlib import Path


logger = logging.getLogger(__name__)


def perf_check(make_changes=True):
    """
    Ensure that the host system is suitably configured to run perf. Will print
    warnings if not.

    Should be run as a superuser if you want to actually change values.
    """
    def get_file_int(path: str) -> int:
        return int(Path(path).read_text().strip())

    if get_file_int('/proc/sys/kernel/kptr_restrict') != 0:
        logger.warn('kptr_restrict value not set for perf')

        if make_changes:
            subprocess.run('sysctl -w kernel.kptr_restrict=0')
            logger.info('set kptr_restrict=0')

    if get_file_int('/proc/sys/kernel/perf_event_max_sample_rate') < 2000:
        logger.warn('perf_event_max_sample_rate is below 2000')

        if make_changes:
            subprocess.run('sysctl -w kernel.perf_event_max_sample_rate=2000')
            logger.info('set perf_event_max_sample_rate=2000')

    if get_file_int('/proc/sys/kernel/perf_event_paranoid') != -1:
        logger.warn('perf_event_paranoid is set to disallow non-root perf use')

        if make_changes:
            subprocess.run('sysctl -w kernel.perf_event_paranoid=-1')
            logger.info('set perf_event_paranoid=-1')


def perf_check_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '-w', action='store_true',
        help=('If given write the preferred sysctl values for perf. '
              'Requires sudo.'))

    args = parser.parse_args()
    perf_check(args.w)
