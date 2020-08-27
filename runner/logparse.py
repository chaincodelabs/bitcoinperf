"""
Tools for parsing bitcoin logs.
"""

import datetime
import re
import typing as t


class FlushEvent(t.NamedTuple):
    relative_time: float
    duration_secs: float
    flushed_count: int
    flushed_kb: int


DATETIME_REGEX = '%Y-%m-%dT%H:%M:%SZ'
FLUSHED_LINE_REGEX = re.compile(
    r'FlushStateToDisk: write coins cache to disk '
    r'\((?P<count>\d+) coins, (?P<kb>\d+)kB\) completed \((?P<secs>\d+\.\d+)s\)')


# 2019-09-20T17:22:33Z
def parse_date(in_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(in_str, DATETIME_REGEX)


def get_log_start(filehandle) -> datetime.datetime:
    line = ''
    for line in filehandle:
        line = line.strip()
        if re.search(r'^\d{4}-\d{2}-\d{2}T', line):
            break

    return parse_date(line.split()[0])


def get_flush_times(filehandle) -> t.List[FlushEvent]:
    start_time = get_log_start(filehandle)
    filehandle.seek(0)
    times = []

    for line in filehandle:
        match = FLUSHED_LINE_REGEX.search(line)

        if match:
            groups = match.groupdict()
            line_time = parse_date(line.split()[0])

            times.append(FlushEvent(
                relative_time=(line_time - start_time).total_seconds(),
                duration_secs=float(groups['secs']),
                flushed_count=int(groups['count']),
                flushed_kb=int(groups['kb']),
            ))

    return times
