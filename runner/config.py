import socket
import os
import multiprocessing
import re
import tempfile
import typing as t
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, validator, PositiveInt
import yaml

from . import logging

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


def is_writeable_path(p: str):
    if not os.access(Path(p).parent, os.W_OK):
        raise ValueError("path {} is not writable".format(p))
    return Path(p)


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


def _expandvars(s: str):
    if isinstance(s, str):
        return os.path.expandvars(s)
    return s


class EnvStr(str):
    @classmethod
    def __get_validators__(cls):
        yield _expandvars


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


class WriteablePath(Path):
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield is_writeable_path


class SyncedPeer(BaseModel):
    datadir: ExistingDatadir = ''
    repodir: BuiltRepoDir = ''
    bitcoind_extra_args: str = ''
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
        'bench-ssd-6': 'ccl-bench-ssd-6',
    }.get(HOSTNAME, '')


class Codespeed(BaseModel):
    url: EnvStr
    username: EnvStr
    password: EnvStr
    envname: EnvStr = None

    @validator('envname', always=True)
    def infer_envname(cls, v):
        return v or get_envname()


class Bench(BaseModel):
    enabled: bool = True
    run_count: PositiveInt = 1


class BenchBuild(Bench):
    num_jobs: t.Op[PositiveInt] = DEFAULT_NPROC
    configure_args: EnvStr = ""


class BenchUnittests(Bench):
    num_jobs: t.Op[PositiveInt] = DEFAULT_NPROC


class BenchFunctests(Bench):
    num_jobs: t.Op[PositiveInt] = DEFAULT_NPROC


class BenchMicrobench(Bench):
    filter: str = ''


class BenchIbdFromNetwork(Bench):
    start_height: PositiveInt = 0
    end_height: t.Op[PositiveInt] = None
    time_heights: t.Op[t.List[PositiveInt]] = None
    stash_datadir: t.Op[WriteablePath] = None


class BenchIbdFromLocal(Bench):
    start_height: PositiveInt = 0
    end_height: t.Op[PositiveInt] = None
    time_heights: t.Op[t.List[PositiveInt]] = None
    stash_datadir: t.Op[WriteablePath] = None


class BenchIbdRangeFromLocal(Bench):
    src_datadir: ExistingDatadir
    start_height: PositiveInt = 0
    end_height: t.Op[PositiveInt]
    time_heights: t.Op[t.List[PositiveInt]] = None


class BenchReindex(Bench):
    # TODO:
    # If None, we'll use the resulting datadir from the previous benchmark.
    src_datadir: t.Op[Path] = None
    start_height: PositiveInt = 0
    end_height: PositiveInt = None
    time_heights: t.Op[t.List[PositiveInt]] = None
    stash_datadir: t.Op[WriteablePath] = None


class BenchReindexChainstate(Bench):
    # TODO:
    # If None, we'll use the resulting datadir from the previous benchmark.
    src_datadir: t.Op[Path] = None
    start_height: PositiveInt = 0
    end_height: PositiveInt = None
    time_heights: t.Op[t.List[PositiveInt]] = None
    stash_datadir: t.Op[WriteablePath] = None


class Benches(BaseModel):
    build: t.Op[BenchBuild] = None
    unittests: t.Op[BenchUnittests] = None
    functests: t.Op[BenchFunctests] = None
    microbench: t.Op[BenchMicrobench] = None
    ibd_from_network: t.Op[BenchIbdFromNetwork] = None
    ibd_from_local: t.Op[BenchIbdFromLocal] = None
    ibd_range_from_local: t.Op[BenchIbdRangeFromLocal] = None
    reindex: t.Op[BenchReindex] = None
    reindex_chainstate: t.Op[BenchReindexChainstate] = None


class Target(BaseModel):
    gitref: EnvStr
    gitremote: EnvStr = "origin"
    bitcoind_extra_args: EnvStr = ""

    # Used for display in output.
    name: EnvStr = ""

    # If True, rebase this branch on top of latest master.
    rebase: bool = True

    @property
    def id(self):
        return "{}-{}".format(
            self.gitref,
            re.sub(r'\s+', '', self.bitcoind_extra_args).replace('-', ''))

    @validator('name', always=True)
    def make_name(cls, v, values, **kwargs):
        if not v:
            return values['gitref']
        return v

    def __hash__(self):
        return hash(
            self.gitref + self.gitremote + self.bitcoind_extra_args +
            self.name)


class Compilers(str, Enum):
    clang = 'clang'
    gcc = 'gcc'


class Slack(BaseModel):
    webhook_url: EnvStr = None


class Config(BaseModel):
    workdir: Path = None
    synced_peer: SyncedPeer = None
    compilers: t.List[Compilers] = [Compilers.clang, Compilers.gcc]
    slack: Slack = None
    log_level: str = 'INFO'
    teardown: bool = True
    safety_checks: bool = True
    clean: bool = True
    cache_build: bool = False
    cache_git: bool = False
    cache_build_size: int = 5
    codespeed: Codespeed = None
    benches: Benches = None
    to_bench: t.List[Target]

    @validator('workdir', pre=True, always=True)
    def mk_workdir(cls, v):
        if not v:
            return Path(tempfile.mkdtemp(prefix='bitcoinperf-'))
        return Path(v)

    @validator('benches', whole=True)
    def check_peer(cls, v, values, **kwargs):
        if v.ibd_from_network or v.ibd_from_local or v.ibd_range_from_local \
                or v.reindex or v.reindex_chainstate:
            if not values.get('synced_peer'):
                raise ValueError(
                    "synced_peer must be specified when running "
                    "IBD- or reindex-based benchmarks")

        return v

    def bitcoinperf_home_path(self):
        p = Path.home() / '.bitcoinperf'
        p.mkdir(exist_ok=True)
        return p

    def build_cache_path(self):
        p = self.bitcoinperf_home_path() / 'build_cache'
        p.mkdir(exist_ok=True, parents=True)
        return p

    @property
    def results_dir(self):
        d = self.workdir / 'results'
        d.mkdir(exist_ok=True)
        return d


def load(content: t.Union[Path, str]) -> Config:
    if isinstance(content, Path):
        content = content.read_text()

    return Config(**yaml.load(content), Loader=yaml.Loader)
