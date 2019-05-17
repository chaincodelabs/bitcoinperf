from collections import defaultdict

import requests

from .config import Target
from .globals import GitCheckout
from .logging import get_logger

logger = get_logger()


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
    total_time: int = None


class HeightData(t.NamedTuple):
    time_secs: float
    rss_kb: int
    cpu_percentage: float
    num_fds: int


@dataclass
class IbdResults(Results):
    height_to_data: t.Dict[int, HeightData]


all_results: t.Dict[Target, t.Dict[t.Type['Benchmark'], t.List[Results]]] = {}


def save_result(bench_instance):
    pass


def save_result(gitco: GitCheckout,
                benchmark_name: str,
                total_secs: float,
                memusage_kib: float,
                executable: str,
                extra_data: dict = None):
    """Save a result, forwarding it to all reporters."""
    REF_TO_NAME_TO_TIME[gitco.ref][benchmark_name].append(total_secs)

    for reporter in reporters:
        try:
            reporter.save_result(
                benchmark_name, total_secs, executable, extra_data)
        except Exception:
            logger.exception("failed to save result with %s", reporter)

    # This may be called before the command has completed (in the case of
    # incremental IBD reports), so only report memory usage if we have
    # access to it.
    if memusage_kib is not None:
        mem_name = benchmark_name + '.mem-usage'
        REF_TO_NAME_TO_TIME[gitco.ref][mem_name].append(memusage_kib)

        for reporter in reporters:
            reporter.save_result(
                mem_name, memusage_kib, executable, extra_data,
                units_title='Size', units='KiB')


class Reporter:
    """Abstract interface for reporting results."""
    def save_result(self, gitco: GitCheckout, benchmark_name, value,
                    executable,
                    extra_data=None, units_title=None, units=None):
        pass


class LogReporter:
    """Log results."""
    def save_result(self, *args, **kwargs):
        resstr = "result: "
        resstr += ",".join(str(i) for i in args)
        resstr += ",".join(str(i) for i in kwargs.values())
        logger.info(resstr)


class CodespeedReporter:
    """Report results to codespeed."""
    def __init__(self, codespeed_cfg):
        self.server_url = codespeed_cfg.url
        self.codespeed_envname = codespeed_cfg.envname
        self.username = codespeed_cfg.username
        self.password = codespeed_cfg.password

    def save_result(self,
                    gitco: GitCheckout, benchmark_name, value, executable,
                    extra_data=None, units_title=None, units=None):
        self.send_to_codespeed(
            gitco, benchmark_name, value, executable,
            extra_data=extra_data,
            result_max=extra_data.pop('result_max', None),
            result_min=extra_data.pop('result_min', None),
        )

    def send_to_codespeed(
            self,
            gitco: GitCheckout,
            bench_name, result, executable,
            lessisbetter=True, units_title='Time', units='seconds',
            description='', result_max=None, result_min=None, extra_data=None):
        """
        Send a benchmark result to codespeed over HTTP.
        """
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

        resp = requests.post(
            self.server_url + '/result/add/',
            data=data, auth=(self.username, self.password))

        if resp.status_code != 202:
            raise ValueError(
                'Request to codespeed returned an error %s, '
                'the response is:\n%s'
                % (resp.status_code, resp.text)
            )
