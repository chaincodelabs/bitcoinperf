import argparse
import socket
import os
import sys
import datetime
import multiprocessing

from . import logging

# Get physical memory specs
MEM_GIB = (
    os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024. ** 3))

HOSTNAME = socket.gethostname()
BENCH_NAMES = {
    'gitclone', 'build', 'makecheck', 'functionaltests',
    'microbench', 'ibd', 'reindex'}


class RunData:
    """
    Data set at runtime during benchmarking.

    Attached to `args` and passed around that way.
    """
    gitsha = None

    # The non-SHA name of the ref being tested.
    gitref = None

    # The working directory for this benchmark. Contains a `bitcoin/` subdir.
    workdir = None

    # Did we acquire the benchmarking lockfile?
    lockfile_acquired = False

    compiler = None


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

    def name_to_count_type(s):
        if not s:
            return {}
        out = {}
        for i in s.split(','):
            name, num = i.split(':')
            out[name] = int(num)
        return out

    addarg('REPO_LOCATION', 'https://github.com/bitcoin/bitcoin.git')
    addarg('REPO_BRANCH', 'master', 'The branch to test')
    addarg('WORKDIR', '',
           'Path to where the temporary bitcoin clone will be checked out')
    addarg('IBD_PEER_ADDRESS', '',
           'Network address to synced peer to IBD from. If left blank, '
           'IBD will be done from the mainnet P2P network.')
    addarg('SYNCED_DATA_DIR', '',
           'When using a local IBD peer, specify a path to a datadir synced '
           'to a chain high enough to do the requested IBD '
           '(see --bitcoind-stopatheight)')
    addarg('SYNCED_BITCOIN_REPO_DIR', os.environ['HOME'] + '/bitcoin',
           'Where the bitcoind binary which will serve blocks for IBD lives')
    addarg('SYNCED_BITCOIND_ARGS', '',
           'Additional arguments to pass to the bitcoind invocation for '
           'the synced IBD peer, e.g. -minimumchainwork')
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
    addarg('BITCOIND_STOPATHEIGHT', '522000')
    addarg('BITCOIND_ASSUMEVALID',
           '000000000000000000176c192f42ad13ab159fdb20198b87e7ba3c001e47b876',
           help=('Should be set to a known block (e.g. the block hash of '
                 'BITCOIND_STOPATHEIGHT) to make sure it is not set to a '
                 'future block that we are not aware of'))
    addarg('BITCOIND_PORT', '9003')
    addarg('BITCOIND_RPCPORT', '9004')
    addarg('LOG_LEVEL', 'DEBUG')
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
    addarg('CODESPEED_USER', '')
    addarg('CODESPEED_PASSWORD', '')
    addarg('CODESPEED_ENVNAME', {
        'bench-odroid-1': 'ccl-bench-odroid-1',
        'bench-raspi-1': 'ccl-bench-raspi-1',
        'bench-hdd-1': 'ccl-bench-hdd-1',
        'bench-ssd-1': 'ccl-bench-ssd-1',
    }.get(HOSTNAME))

    return parser


def parse_args(*args, **kwargs):
    parser = build_parser()
    args = parser.parse_args(*args, **kwargs)
    args.benches_to_run = list(filter(None, args.benches_to_run))
    args.compilers = list(sorted(args.compilers))

    args.logger = logging.get_logger(args.log_level)
    args.run_data = RunData()
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
        args.logger.info(
            "Running synced chain node on localhost "
            "(no remote addr specified)")
    elif not args.ibd_peer_address:
        args.ibd_from_network = True
        args.logger.info(
            "Running a REAL IBD from the P2P network. "
            "This may result in inconsistent IBD times.")

    if args.codespeed_url:
        assert(args.codespeed_user)
        assert(args.codespeed_password)
        assert(args.codespeed_envname)

    for name in args.benches_to_run:
        if name not in BENCH_NAMES:
            print("Unrecognized bench name %r" % name)
            sys.exit(1)

    for comp in args.compilers:
        if comp not in {'gcc', 'clang'}:
            print("Unrecognized compiler name %r" % comp)
            sys.exit(1)

    return args
