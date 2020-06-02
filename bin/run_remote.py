#!/usr/bin/env python3.8
"""
Usage:
    <hostname ...>

Where:
    <hostname>  Hostname to run bench on.
"""
import sys
import argparse
import itertools
import os
import datetime
import subprocess
import re
import textwrap
import time
from collections import namedtuple
from pathlib import Path
from multiprocessing.dummy import Pool
import typing as t

import mitogen


def host_entrypoint(count, args) -> dict:
    """
    The function that is run on each host being acted on. This basically
    routes the args into the action that should be taken.

    Returns a dict mapping branch names to lists of timing data.
    """
    func = {
        'cmd': run_cmd,
        'reindex': run_reindex,
        'au': run_au,
    }[args['funcname']]

    if func == run_cmd:
        return run_cmd(args['cmd'], args)

    elif func not in [run_reindex, run_au]:
        raise RuntimeError("Func not recognized!")

    os.chdir(Path.home() / 'bitcoin')
    setup_git_remotes()

    return func(count, args)


def setup_git_remotes():
    remotes = {
        i.split()[0] for i in
        run('git remote -v').stdout.decode().splitlines()}

    def get_remote(name):
        if name not in remotes:
            run(f'git remote add {name} https://github.com/{name}/bitcoin.git')
        run(f'git fetch {name} --tags')

    get_remote('martinus')
    get_remote('jamesob')
    get_remote('laanwj')


def get_branch_list(seed: int, branches: list):
    """
    Args:
        seed: used to determine how to permute the branch order
    """
    perms = itertools.cycle(itertools.permutations(branches))

    for i in range(seed):
        next(perms)
    bench_order = list(next(perms))

    # Run everything twice.
    return bench_order * 2


def checkout_and_build(ref):
    # ac831339cb is an arbitrary commit known to exist in master. We
    # check that out temporarily to avoid switching to whichever branch
    # we'd be deleting.
    runmany(f"""
        git checkout ac831339cb
        git branch -D {ref} || true
        git checkout {ref}
        make clean && make -j $(nproc --ignore=1)
    """)
    run(
        'sync; sudo /sbin/swapoff -a; sudo /sbin/sysctl vm.drop_caches=3; ',
        # 'sudo /usr/local/bin/pyperf system tune; ',
        check=False,
    )


def run_reindex(seed, args):
    branches = [
        # 'martinus/2019-09-SaltedOutpointHasher-noexcept',
        # 'jamesob/2019-08-robinhood',

        # This is the unmodified robinhood impl. (with its hash mixin)
        # '6f9882ce4a817c9f14aa7526165ab6e278de890e',

        # 'master',
        # 'martinus/2019-08-bulkpoolallocator',
        # 'martinus/2019-09-more-compact-Coin',
        # 'bench/au.master.1',
        # 'bench/alloc.1',
        # 'bench/robinhood.1',
        # 'bench/au.1',
        # 'b4a1da9ef8e4b673c290d5b882527e627ae1b43a',
        # 'laanwj/2019_11_prevector',
        '2019-12-partial-flush',
        'e354db787790b84b0b3f34cc55b65446c71e4fa2', # base of partial-flush
    ]

    outd = {}
    bench_order = get_branch_list(seed, branches)
    for i in set(bench_order):
        outd[i] = []
    print(bench_order)

    dbcache = args['dbcache']
    cmd = (
        f'./src/bitcoind -reindex-chainstate -stopatheight=550000 '
        f'-dbcache={dbcache} -connect=0')

    for ref in bench_order:
        checkout_and_build(ref)

        r = run(f'/usr/bin/time -v {cmd}')
        outlines = [i.strip() for i in r.stderr.decode().splitlines()]
        result = _parse_time_output(dict(i.split(': ') for i in outlines))
        print("Finished {}: {}".format(ref, result))
        result['dbcache'] = args['dbcache']
        result['cmd'] = cmd
        outd[ref].append(result)

    return outd


def _parse_time_output(outd):
    return {
        'time': outd['Elapsed (wall clock) time (h:mm:ss or m:ss)'],
        'cpu_perc': outd['Percent of CPU this job got'],
        'mem_kb': int(outd['Maximum resident set size (kbytes)']),
        'user_time_secs': float(outd['User time (seconds)']),
        'system_time_secs': float(outd['System time (seconds)']),
    }


def run_au(seed, args):
    """Time syncing to tip from loading a 600k UTXO snapshot."""
    branches = [
        # 'utxo-dumpload-compressed',
        'utxo-dumpload.54',
        # 'bench/au.no-erase',
        'bench/au.no-erase.1',
    ]

    outd = {}
    bench_order = get_branch_list(seed, branches)
    for i in set(bench_order):
        outd[i] = []
    print(bench_order)

    for ref in bench_order:
        checkout_and_build(ref)
        stop_block = 604_667
        dbcache = args.get('dbcache', 5000)
        # This was downloaded manually beforehand.
        snapshot_path = '/tmp/utxo.dat'
        datadir = '/tmp/utxo-datadir'

        run(f'rm -rf {datadir}; mkdir {datadir}')
        cmd = (
            f'./src/bitcoind -datadir={datadir} -stopatheight={stop_block} '
            f'-dbcache={dbcache} -printtoconsole=0')
        proc = run_async(f'/usr/bin/time -v {cmd}')
        time.sleep(60)
        run(f'./src/bitcoin-cli '
            f'-datadir={datadir} loadtxoutset {snapshot_path}')
        (_, stderr) = proc.communicate()

        outlines = [i.strip() for i in stderr.splitlines()]
        result = _parse_time_output(dict(i.split(': ') for i in outlines))
        print("Finished {}: {}".format(ref, result))
        result['dbcache'] = dbcache
        result['cmd'] = cmd
        outd[ref].append(result)

    return outd


def run_cmd(cmd, *args):
    r = run(f'{cmd}')
    print(r.stdout)
    return


def install_pyperf():
    runmany("""
        sudo python3.8 -m pip install pyperf
        echo 'ccl     ALL=(ALL) NOPASSWD:/usr/local/bin/pyperf system tune' \
            | sudo tee -a /etc/sudoers
    """)


def run_on_host(router, hostname, count, args):
    outd = {}
    print('Running on host {}'.format(hostname))

    creds = (
        {'username': 'ccl', 'password': os.environ.get('CCL_PASSWORD')}
        if hostname != 'bench-strong' else {}
    )
    context = router.ssh(hostname=hostname,
                         check_host_keys='ignore',
                         python_path=['/usr/local/bin/python3.8'],
                         **creds,
                         )

    outd[hostname] = context.call(host_entrypoint, count, args)
    print('Completed bench on host {}'.format(hostname))

    return outd


@mitogen.main()
def main(router):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    parser_reindex = subparsers.add_parser(
        'reindex', help='run a reindex benchmark')
    parser_reindex.add_argument('--dbcache', type=int, default=4000)
    parser_reindex.add_argument('--hosts', nargs='+')
    parser_reindex.set_defaults(funcname='reindex')

    parser_au = subparsers.add_parser(
        'au', help='run an assumeutxo sync benchmark')
    parser_au.add_argument('--hosts', nargs='+')
    parser_au.add_argument('--dbcache', type=int, default=5000)
    parser_au.set_defaults(funcname='au')

    parser_cmd = subparsers.add_parser(
        'cmd', help='run some arbitrary command on each host')
    print(parser_cmd.add_argument('cmd', type=str, default=None))
    parser_cmd.add_argument('--hosts', nargs='+')
    parser_cmd.set_defaults(funcname='cmd')

    args = vars(parser.parse_args())
    print(args)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    results = []
    hostname_to_results = {}

    with Pool(10) as p:
        for i, hostname in enumerate(args['hosts']):
            results.append(p.apply_async(
                run_on_host, (router, hostname, i, args)))

        p.close()
        p.join()

        for r in results:
            hostname_to_results.update(r.get())

    print(hostname_to_results)
    now = datetime.datetime.now().isoformat()

    if args.get('funcname') == 'reindex':
        Path(f'bench_reindex.{now}.out').write_text(str(hostname_to_results))


class RunReturn(namedtuple('RunReturn', 'args,returncode,stdout,stderr')):

    @property
    def ok(self):
        return self.returncode == 0

    @classmethod
    def from_std(cls, cp: subprocess.CompletedProcess):
        return cls(cp.args, cp.returncode, cp.stdout, cp.stderr)


def run(cmd: str, check: bool = True) -> RunReturn:
    print(cmd)
    r = RunReturn.from_std(subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))

    if check and not r.ok:
        print(
            "Command failed (code {}): {}\nstdout:\n{}\n\nstderr:{}\n"
            .format(r.returncode, cmd, r.stdout, r.stderr))

    return r


def run_async(cmd: str) -> subprocess.Popen:
    print(cmd)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=True, text=True)


CmdStrs = t.Union[str, t.Iterable[str]]


def runmany(cmds: CmdStrs, check: bool = True) -> t.List[RunReturn]:
    out = []

    for cmd in _split_cmd_input(cmds):
        r = run(cmd)
        out.append(r)

        if check and not r.ok:
            break

    return out


def _split_cmd_input(cmds: t.Union[list, str]) -> t.List[str]:
    if isinstance(cmds, list):
        return cmds
    cmds = textwrap.dedent(cmds)
    # Eat linebreaks
    cmds = re.sub(r'\s+\\\n\s+', ' ', cmds)
    return [i.strip() for i in cmds.splitlines() if i]
