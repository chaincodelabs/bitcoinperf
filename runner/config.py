import argparse
import socket
import os
import sys
import datetime
import multiprocessing
import re
import tempfile
import typing as t
from pathlib import Path

from marshmallow import Schema, fields, validates_schema
from marshmallow.exceptions import ValidationError
import yaml

from . import logging, results, slack

logger = logging.get_logger()

# Get physical memory specs
MEM_GIB = (
    os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024. ** 3))

HOSTNAME = socket.gethostname()
BENCH_NAMES = {
    'gitclone', 'build', 'makecheck', 'functionaltests',
    'microbench', 'ibd', 'reindex'}


def is_datadir(path):
    if not ((path / 'blocks').exists() and (path / 'chainstate').exists()):
        raise ValidationError("path isn't a valid datadir")

def path_exists(path):
    if not path.exists():
        raise ValidationError("path doesn't exist")


def is_built_bitcoin(path):
    if not ((path / 'src' / 'bitcoind').exists() and
            (path / 'src' / 'bitcoind').exists()):
        raise ValidationError("path doesn't have bitcoin binaries")


def is_compiler(name):
    if name not in ('gcc', 'clang'):
        raise ValidationError("compiler not recognized")


class NetworkAddr(t.NamedTuple):
    host: str
    port: int


class NetworkAddrField(fields.Field):
    def _deserialize(self, val, attr, data, **kwargs):
        host, port = val.split(':')
        return NetworkAddr(host, int(port))

    def _serialize(self, val, attr, obj, **kwargs):
        return "{}:{}".format(val.host, val.port)


class PathField(fields.Field):
    def _deserialize(self, val, attr, data, **kwargs):
        return Path(os.path.expandvars(val))

    def _serialize(self, val, attr, obj, **kwargs):
        return str(val)


class SyncedPeer(Schema):
    datadir = PathField(required=False, validate=[path_exists, is_datadir])
    repodir = PathField(required=False, validate=[path_exists, is_built_bitcoin])
    # or
    address = NetworkAddrField(required=False)


    @validates_schema
    def validate_either_or(self, data):
        if not (set(data.keys()).issuperset({'datadir', 'repodir'}) or
                'address' in data):
            raise ValidationError("synced_peer config not valid")


def create_workdir():
    return tempfile.TemporaryDirectory(prefix='bitcoinperf')

def get_envname():
    return {
        'bench-odroid-1': 'ccl-bench-odroid-1',
        'bench-raspi-1': 'ccl-bench-raspi-1',
        'bench-hdd-1': 'ccl-bench-hdd-1',
        'bench-ssd-1': 'ccl-bench-ssd-1',
    }.get(HOSTNAME, '')


class Codespeed(Schema):
    url = fields.Url()
    username = fields.Str()
    password = fields.Str()
    envname = fields.Str(default=get_envname)


class Slack(Schema):
    webhook_url = fields.Url()


class Bench(Schema):
    enabled = fields.Boolean(default=True)
    run_count = fields.Int(default=1)


class BenchUnittests(Bench):
    num_jobs = fields.Int(default=1)


class BenchFunctests(Bench):
    num_jobs = fields.Int(default=1)


class BenchIbdFromNetwork(Bench):
    start_height = fields.Int(default=0)
    end_height = fields.Int(default=None)
    time_heights = fields.List(fields.Int())


class BenchIbdFromLocal(Bench):
    start_height = fields.Int(default=0)
    end_height = fields.Int(default=None)
    time_heights = fields.List(fields.Int())


class BenchIbdRangeFromLocal(Bench):
    start_height = fields.Int(default=0)
    end_height = fields.Int(default=None)
    time_heights = fields.List(fields.Int())
    src_datadir = PathField(required=True, validate=[path_exists, is_datadir])


class BenchReindex(Bench):
    src_datadir = PathField(validate=[path_exists, is_datadir])


class BenchReindexChainstate(Bench):
    src_datadir = PathField(validate=[path_exists, is_datadir])


class Benches(Schema):
    unittests = fields.Nested(BenchUnittests())
    functests = fields.Nested(BenchFunctests())
    ibd_from_network = fields.Nested(BenchIbdFromNetwork())
    ibd_from_local = fields.Nested(BenchIbdFromLocal())
    ibd_range_from_local = fields.Nested(BenchIbdRangeFromLocal())
    reindex = fields.Nested(BenchReindex())
    reindex_chainstate = fields.Nested(BenchReindexChainstate())


class Target(t.NamedTuple):
    gitref = fields.Str(required=True)
    bitcoind_extra_args = fields.Str()
    configure_args = fields.Str()

    @property
    def id(self):
        return "{}-{}".format(
            self.gitref,
            re.sub('\s+', '', self.bitcoind_extra_args).replace('-', ''))


class Config(Schema):
    workdir = PathField(default=create_workdir)
    artifact_dir = PathField()
    synced_peer = fields.Nested(SyncedPeer)
    codespeed_url = fields.Url()
    compilers = fields.List(fields.String(), validate=is_compiler)
    slack_webhook_url = fields.Url()
    log_level = fields.String(default='INFO')
    nproc = fields.Int(default=min(4, int(multiprocessing.cpu_count())))
    no_teardown = fields.Boolean(default=False)
    no_caution = fields.Boolean(default=False)
    no_clean = fields.Boolean(default=False)
    cache_build = fields.Boolean(default=False)
    codespeed = fields.Nested(Codespeed)
    slack = fields.Nested(Slack)
    benches = fields.Nested(Benches, required=True)
    to_bench = fields.Dict(keys=fields.Str(), values=fields.Nested(Target), required=True)


def load(content: str):
    yam = yaml.load(content)
    for target in yam['to_bench']:
        if not yam['to_bench'][target]:
            yam['to_bench'][target] = {}
        yam['to_bench'][target]['gitref'] = target

    import pprint; pprint.pprint(yam)

    ns = Namespace()
    return Config().load(yam).data


def build_parser():
    parser = argparse.ArgumentParser(description="""
    Run a series of benchmarks against a particular Bitcoin Core revision.

    See bin/run_bench for a sample invocation.

    """)

    def addarg(name, default, help='', *, type=str):
        if not name.isupper() or '-' in name:
            raise ValueError("Argument name should be passed in LIKE_THIS")

        flag_name = name.lower().replace('_', '-')
        default = os.environ.get(name, default)
        if help and not help.endswith('.'):
            help += '.'
        parser.add_argument(
            '--%s' % flag_name, default=default,
            help='{} Default overriden by {} env var (default: {})'.format(
                help, name, default), type=type)

    def csv_type(s):
        return s.split(',')

    def path_type(p):
        if not p:
            return None
        else:
            return Path(p)

    def name_to_count_type(s):
        if not s:
            return {}
        out = {}
        for i in s.split(','):
            name, num = i.split(':')
            out[name] = int(num)
        return out

    addarg('WORKDIR', '',
           'Path to where the temporary bitcoin clone will be checked out')

    addarg('IBD_PEER_ADDRESS', '',
           'Network address to synced peer to IBD from. If left blank, '
           'IBD will be done from the mainnet P2P network.')

    addarg('SYNCED_DATADIR', '',
           'When using a local IBD peer, specify a path to a datadir synced '
           'to a chain high enough to do the requested IBD.',
           type=path_type)

    addarg('COPY_FROM_DATADIR', '',
           'Initialize the downloading peer with a datadir from this path. '
           'The datadir will be copied on disk. Useful for starting from a '
           'non-trivial height using a pruned datadir.',
           type=path_type)

    addarg('SYNCED_BITCOIN_REPO_DIR', os.environ['HOME'] + '/bitcoin',
           'Where the bitcoind binary which will serve blocks for IBD lives',
           type=path_type)

    addarg('CLIENT_BITCOIND_ARGS', '',
           'Additional arguments to pass to the bitcoind invocation for '
           'the downloading client node, e.g. "-prune=10000"')

    addarg('SYNCED_BITCOIND_ARGS', '',
           'Additional arguments to pass to the bitcoind invocation for '
           'the synced IBD peer, e.g. -minimumchainwork')

    addarg('SYNCED_BITCOIND_RPCPORT', '8332',
           'The RPC port the synced node will respond on')

    addarg('IBD_CHECKPOINTS',
           '100_000,200_000,300_000,400_000,500_000,522_000,tip',
           'Chain heights at which duration measurements will be reported '
           'to codespeed. Can include underscores. E.g. 100_000,200_000')

    addarg('CODESPEED_URL', '')

    addarg('SLACK_WEBHOOK_URL', '')

    addarg(
        'RUN_COUNTS', '',
        help=(
            "Specify the number of times a benchmark should be run, e.g. "
            "'ibd:3,microbench:2'"),
        type=name_to_count_type)

    addarg(
        'BENCHES_TO_RUN', default=','.join(BENCH_NAMES),
        help='Only run a subset of benchmarks',
        type=csv_type)

    addarg('COMPILERS', 'clang,gcc', type=csv_type)

    addarg('MAKE_JOBS', '1', type=int)

    addarg(
        'COMMITS', '',
        help=("The branches, tags, or commits to test, e.g. "
              "'master,my_change'"),
        type=csv_type)

    addarg('BITCOIND_DBCACHE', '2048' if MEM_GIB > 3 else '512')

    addarg('BITCOIND_ASSUMEVALID',
           '000000000000000000176c192f42ad13ab159fdb20198b87e7ba3c001e47b876',
           help=('Should be set to a known block (e.g. the block hash of '
                 'BITCOIND_STOPATHEIGHT) to make sure it is not set to a '
                 'future block that we are not aware of'))

    addarg('BITCOIND_PORT', '9003')

    addarg('BITCOIND_RPCPORT', '9004')

    addarg('LOG_LEVEL', 'INFO')

    addarg('NPROC', min(4, int(multiprocessing.cpu_count())), type=int)

    addarg('NO_TEARDOWN', False,
           'If true, leave the Bitcoin checkout intact after finishing',
           type=bool)

    addarg('NO_CAUTION', False,
           "If true, don't perform a variety of startup checks and cache "
           "drops",
           type=bool)

    addarg('NO_CLEAN', False,
           "If true, do not call `make distclean` before builds. Useful for "
           "when you don't care about build times.", type=bool)

    addarg('USE_BUILD_CACHE', False,
           "If true, cache the builds per commit (useful for testing) under "
           "~/.bitcoinperf",
           type=bool)

    addarg('CODESPEED_USER', '')

    addarg('CODESPEED_PASSWORD', '')

    addarg('CODESPEED_ENVNAME', {
        'bench-odroid-1': 'ccl-bench-odroid-1',
        'bench-raspi-1': 'ccl-bench-raspi-1',
        'bench-hdd-1': 'ccl-bench-hdd-1',
        'bench-ssd-1': 'ccl-bench-ssd-1',
    }.get(HOSTNAME, ''))

    return parser


def parse_args(*args, **kwargs):
    parser = build_parser()
    args = parser.parse_args(*args, **kwargs)
    args.benches_to_run = list(filter(None, args.benches_to_run))
    args.compilers = list(sorted(args.compilers))

    logging.configure_logger(args.log_level)
    args.running_synced_bitcoind_locally = False

    args.bench_prefix = (
        "bench-%s-%s-" %
        (args.repo_branch,
         datetime.datetime.utcnow().strftime('%Y-%m-%d')))

    # True when running an IBD from random peers on the network, i.e. a "real"
    # IBD.
    args.ibd_from_network = False

    if args.ibd_peer_address in ('localhost', '127.0.0.1', '0.0.0.0'):
        args.running_synced_bitcoind_locally = True
        args.ibd_peer_address = '127.0.0.1'
        logger.info(
            "Running synced chain node on localhost "
            "(no remote addr specified)")
    elif not args.ibd_peer_address:
        args.ibd_from_network = True
        logger.info(
            "Running a REAL IBD from the P2P network. "
            "This may result in inconsistent IBD times.")

    args.codespeed_reporter = None

    if args.codespeed_url:
        assert(args.codespeed_user)
        assert(args.codespeed_password)
        assert(args.codespeed_envname)
        args.codespeed_report = results.CodespeedReporter(
            args.codespeed_url,
            args.codespeed_envname,
            args.codespeed_user,
            args.codespeed_password)

    args.slack_client = slack.Client(args.slack_webhook_url)

    for name in args.benches_to_run:
        if name not in BENCH_NAMES:
            print("Unrecognized bench name %r" % name)
            sys.exit(1)

    for comp in args.compilers:
        if comp not in {'gcc', 'clang'}:
            print("Unrecognized compiler name %r" % comp)
            sys.exit(1)

    args.ibd_checkpoints_as_ints = []

    for checkpoint in args.ibd_checkpoints.split(','):
        if checkpoint != 'tip':
            args.ibd_checkpoints_as_ints.append(int(
                checkpoint.replace("_", "")))

    args.ibd_to_tip = 'tip' in args.ibd_checkpoints
    args.last_ibd_checkpoint = (
        args.ibd_checkpoints_as_ints[-1] if
        args.ibd_checkpoints_as_ints else None)

    return args


def get_commits(cfg) -> t.List[t.Tuple[str, str]]:
    cfg.commits = list(filter(None, cfg.commits))

    if not cfg.commits:
        return [('', 'HEAD')]
    commits = []

    for commit in cfg.commits:
        # Allow users to specify commits in different remotes.
        remote = ''
        if ':' in commit:
            remote, commit = commit.split(':')
        commits.append((remote, commit))

    return commits
