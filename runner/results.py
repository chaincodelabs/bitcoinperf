import requests
import typing as t
from dataclasses import dataclass, field

from .git import GitCheckout
from .logging import get_logger
from .logparse import FlushEvent

logger = get_logger()


ALL_RUNS: t.List['Benchmarks'] = []
HWINFO: t.Dict = {}


class Reporters:
    """A container for Reporter instances - to be populated in runner/main"""
    codespeed = None
    log = None


@dataclass
class Results:
    """
    A container for results data. Paired with each Benchmark type in
    `runner.benchmarks`.
    """
    # The primary shell command associated with this benchmark.
    command: str = ''

    # A shortish title suitable for presentation on a graph.
    title: str = ''

    # In seconds
    total_time_secs: float = None
    peak_rss_kb: int = None

    # See hwinfo.parse_configure_log()
    configure_info: dict = None


class HeightData(t.NamedTuple):
    time_secs: float
    rss_kb: int
    cpu_percentage: float
    num_fds: int


@dataclass
class IbdResults(Results):
    height_to_data: t.Dict[int, HeightData] = field(default_factory=dict)

    # When the coins cache is flushed from memory to disk.
    flush_events: t.List[FlushEvent] = field(default_factory=list)


@dataclass
class MicrobenchResults(Results):
    bench_to_time: t.Dict[str, float] = field(default_factory=dict)


def report_result(benchmark,
                  metric_name: str,
                  val: float,
                  *,
                  extra_data: dict = None,
                  report_to_codespeed: bool = True,
                  ):
    """Save a result, forwarding it to all reporters."""
    for reporter in [Reporters.log, Reporters.codespeed]:
        if not reporter:
            continue
        elif reporter == Reporters.codespeed and not report_to_codespeed:
            continue

        units = units_title = None
        if metric_name.endswith('.mem-usage'):
            units = 'KiB'
            units_title = 'Size'

        try:
            reporter.save_result(
                benchmark.gitco, metric_name, val, extra_data,
                units=units, units_title=units_title,
            )
        except Exception:
            logger.exception("failed to save result with %s", reporter)


class Reporter:
    """Abstract interface for reporting results."""
    def save_result(self, gitco: GitCheckout, benchmark_name, value,
                    extra_data=None, units_title=None, units=None):
        pass


class FileReporter:
    def __init__(self, cfg):
        pass

    def save_result(self, gitco, benchmark_name, value, extra_data):
        pass


class CodespeedReporter:
    """Report results to codespeed."""
    def __init__(self, codespeed_cfg):
        self.server_url = codespeed_cfg.url
        self.codespeed_envname = codespeed_cfg.envname
        self.username = codespeed_cfg.username
        self.password = codespeed_cfg.password

    def save_result(self,
                    gitco: GitCheckout, benchmark_name, value,
                    extra_data=None, units_title=None, units=None):
        extra_data = extra_data or {}
        self.send_to_codespeed(
            gitco, benchmark_name, value,
            extra_data=extra_data,
            result_max=extra_data.pop('result_max', None),
            result_min=extra_data.pop('result_min', None),
        )

    def send_to_codespeed(
            self,
            gitco: GitCheckout,
            bench_name, result,
            lessisbetter=True, units_title='Time', units='seconds',
            description='', result_max=None, result_min=None, extra_data=None):
        """
        Send a benchmark result to codespeed over HTTP.
        """
        # This "executable" thing is unique to codespeed and kind of weird, so
        # instead of burdening benchmark code with that, just adapt it here.
        executable_map = {
            'build': 'make',
            'makecheck': 'unittests',
            'functionaltests': 'functional-test-runner',
            'micro.': 'bench-bitcoin',
            'ibd.': 'bitcoind',
            'reindex.': 'bitcoind',
            'reindex_chainstate.': 'bitcoind',
        }

        executable = None

        for prefix, exec_name in executable_map.items():
            if bench_name.startswith(prefix):
                executable = exec_name
                break

        if not executable:
            raise ValueError("unknown executable for metric {}".format(
                bench_name))

        # Mandatory fields
        data = {
            'commitid': gitco.sha,
            'branch': gitco.ref,
            'project': 'Bitcoin Core',
            'executable': executable,
            'benchmark': bench_name,
            'environment': self.codespeed_envname,
            'result_value': result,
            # Optional. Default is taken either from VCS integration or from
            # current date
            # 'revision_date': current_date,
            # 'result_date': current_date,  # Optional, default is current date
            # 'std_dev': std_dev,  # Optional. Default is blank
            'max': result_max,  # Optional. Default is blank
            'min': result_min,  # Optional. Default is blank
            # Ignored if bench_name already exists:
            'lessisbetter': lessisbetter,
            'units_title': units_title,
            'units': units,
            'description': description,
            'extra_data': extra_data or {},
        }

        logger.debug(
            "Attempting to send benchmark (%s, %s) to codespeed",
            bench_name, result)

        if not self.server_url:
            return

        self._result_add_http(data)

        # If the bench being reported is IBD or reindex, report the same result
        # additionally under a different name.
        #
        # This maintains historical compatibility with previous versions of the
        # database when we'd embed the dbcache value in the benchmark name.
        # This allows us to continue showing historical data on grafana
        # dashboards without having to migrate anything. It's shamefully lazy.
        #
        compat_bench_name = None

        if bench_name.startswith('ibd.local') or \
                bench_name.startswith('reindex.'):
            name_split = bench_name.split('.')
            last_digit_idx = 0

            # Insert the dbcache value after the last digit in the name.
            for i, part in enumerate(name_split):
                try:
                    int(part)
                    last_digit_idx = i
                except Exception:
                    pass

            name_split.insert(last_digit_idx + 1, 'dbcache={}'.format(
                extra_data.get('dbcache', None)))
            compat_bench_name = '.'.join(name_split)

        if compat_bench_name:
            data['benchmark'] = compat_bench_name
            self._result_add_http(data)

    def _result_add_http(self, data):
        url = self.server_url + '/result/add/'
        logger.info("Posting data to %s:\n%s", url, data)
        resp = requests.post(
            url, data=data, auth=(self.username, self.password))

        if resp.status_code != 202:
            raise ValueError(
                'Request to codespeed returned an error %s, '
                'the response is:\n%s'
                % (resp.status_code, resp.text)
            )

        return resp
