import socket
import os
import multiprocessing
import re
import tempfile
import typing as t
from argparse import Namespace
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, validator, UrlStr, PositiveInt

from . import logging, slack

logger = logging.get_logger()

t.Op = t.Optional

# Get physical memory specs
MEM_GIB = (
    os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024. ** 3))

DEFAULT_NPROC = min(4, int(multiprocessing.cpu_count()))

HOSTNAME = socket.gethostname()
BENCH_NAMES = {
    'gitclone', 'build', 'makecheck', 'functionaltests',
    'microbench', 'ibd', 'reindex'}


def is_valid_path(p: str):
    return Path(os.path.expandvars(p))


def is_datadir(path: Path):
    if not ((path / 'blocks').exists() and (path / 'chainstate').exists()):
        raise ValueError("path isn't a valid datadir")
    return path


def path_exists(path: Path):
    if not path.exists():
        raise ValueError("path doesn't exist")
    return path


def is_built_bitcoin(path: Path):
    if not ((path / 'src' / 'bitcoind').exists() and
            (path / 'src' / 'bitcoin-cli').exists()):
        raise ValueError("path doesn't have bitcoin binaries")
    return path


def is_compiler(name):
    if name not in ('gcc', 'clang'):
        raise ValueError("compiler not recognized")
    return name


def is_port_open(addr: str) -> bool:
    hostname, port = addr, '8332'
    if ':' in addr:
        hostname, port = addr.split(':')

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((hostname, int(port)))
        s.shutdown(2)
        return addr
    except Exception:
        raise ValueError("can't connect to node at {}".format(addr))


class NodeAddr(str):
    """An address:port string pointing to a running bitcoin node."""
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield is_port_open


class ExistingDatadir(Path):
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield is_valid_path
        yield path_exists
        yield is_datadir


class BuiltRepoDir(Path):
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield is_valid_path
        yield path_exists
        yield is_built_bitcoin


def _create_workdir(p: Path):
    return p or tempfile.TemporaryDirectory(prefix='bitcoinperf')


class Workdir(Path):
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield is_valid_path
        yield _create_workdir


class SyncedPeer(BaseModel):
    datadir: ExistingDatadir = ''
    repodir: BuiltRepoDir = ''
    # or
    address: NodeAddr = None

    # TODO actually use this
    def validate_either_or(self, data):
        if not (set(data.keys()).issuperset({'datadir', 'repodir'}) or
                'address' in data):
            raise ValueError("synced_peer config not valid")


def get_envname():
    return {
        'bench-odroid-1': 'ccl-bench-odroid-1',
        'bench-raspi-1': 'ccl-bench-raspi-1',
        'bench-hdd-1': 'ccl-bench-hdd-1',
        'bench-ssd-1': 'ccl-bench-ssd-1',
    }.get(HOSTNAME, '')


class Codespeed(BaseModel):
    url: UrlStr
    username: str
    password: str
    envname: str = None

    @validator('envname')
    def infer_envname(cls, v):
        return v or get_envname()


class Slack(BaseModel):
    webhook_url: UrlStr


class Bench(BaseModel):
    enabled: bool = True
    run_count: PositiveInt = 1


class BenchUnittests(Bench):
    num_jobs: t.Op[PositiveInt] = DEFAULT_NPROC


class BenchFunctests(Bench):
    num_jobs: t.Op[PositiveInt] = DEFAULT_NPROC


class BenchMicrobench(Bench):
    num_jobs: t.Op[PositiveInt] = DEFAULT_NPROC


class BenchIbdFromNetwork(Bench):
    start_height: PositiveInt = 0
    end_height: t.Op[PositiveInt] = None
    time_heights: t.Op[t.List[PositiveInt]] = None


class BenchIbdFromLocal(Bench):
    start_height: PositiveInt = 0
    end_height: t.Op[PositiveInt] = None
    time_heights: t.Op[t.List[PositiveInt]] = None


class BenchIbdRangeFromLocal(Bench):
    src_datadir: ExistingDatadir
    start_height: PositiveInt = 0
    end_height: t.Op[PositiveInt]
    time_heights: t.Op[t.List[PositiveInt]] = None


class BenchReindex(Bench):
    # If None, we'll use the resulting datadir from the previous benchmark.
    src_datadir: t.Op[ExistingDatadir] = None
    end_height: PositiveInt = None
    time_heights: t.Op[t.List[PositiveInt]] = None


class BenchReindexChainstate(Bench):
    # If None, we'll use the resulting datadir from the previous benchmark.
    src_datadir: t.Op[ExistingDatadir] = None
    end_height: PositiveInt = None
    time_heights: t.Op[t.List[PositiveInt]] = None


class Benches(BaseModel):
    unittests: t.Op[BenchUnittests] = None
    functests: t.Op[BenchFunctests] = None
    microbench: t.Op[BenchMicrobench] = None
    ibd_from_network: t.Op[BenchIbdFromNetwork] = None
    ibd_from_local: t.Op[BenchIbdFromLocal] = None
    ibd_range_from_local: t.Op[BenchIbdRangeFromLocal] = None
    reindex: t.Op[BenchReindex] = None
    reindex_chainstate: t.Op[BenchReindexChainstate] = None


class Target(BaseModel):
    gitref: str
    gitremote: str = ""
    bitcoind_extra_args: str = ""
    configure_args: str = ""

    @property
    def id(self):
        return "{}-{}".format(
            self.gitref,
            re.sub(r'\s+', '', self.bitcoind_extra_args).replace('-', ''))


class Compilers(str, Enum):
    clang = 'clang'
    gcc = 'gcc'


class Slack(BaseModel):
    webhook_url: UrlStr = None

    def get_client(self):
        return slack.Client(self.webhook_url)


class Config(BaseModel):
    workdir: Workdir = ''
    synced_peer: SyncedPeer
    compilers: t.List[Compilers] = [Compilers.clang, Compilers.gcc]
    slack: Slack = None
    log_level: str = 'INFO'
    num_build_jobs: PositiveInt = DEFAULT_NPROC
    no_teardown: bool = False
    no_caution: bool = False
    no_clean: bool = False
    no_cache_drop: bool = False
    cache_build: bool = False
    codespeed: Codespeed = None
    benches: Benches
    to_bench: t.List[Target]

    def build_cache_path(self):
        p = Path.home() / '.bitcoinperf' / 'build_cache'
        p.mkdir(exist_ok=True, parents=True)
        return p


def populate_run_counts(cfg):
    for target in cfg.to_bench:
        counts = {}
        for benchname, benchcfg in cfg.benches.items():
            counts[benchname] = benchcfg.run_count
        G.run_counts[target.gitref] = counts
