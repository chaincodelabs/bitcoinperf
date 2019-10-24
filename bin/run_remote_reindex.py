#!/usr/bin/env python3.7
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
from pathlib import Path
from multiprocessing.dummy import Pool

import mitogen
from fscm import run, runmany


def bench(count, args):
    outd = {}
    to_bench = [
        # 'martinus/2019-09-SaltedOutpointHasher-noexcept',
        # 'jamesob/2019-08-robinhood',

        # This is the unmodified robinhood impl. (with its hash mixin)
        # '6f9882ce4a817c9f14aa7526165ab6e278de890e',

        'master',
        # 'martinus/2019-08-bulkpoolallocator',
        'martinus/2019-09-more-compact-Coin',
    ]

    perms = itertools.cycle(itertools.permutations(to_bench))

    for i in range(count):
        next(perms)
    bench_order = next(perms)

    os.chdir(Path.home() / 'bitcoin')

    remotes = {
        i.split()[0] for i in
        run('git remote -v').stdout.decode().splitlines()}

    if 'jamesob' not in remotes:
        run('git remote add jamesob https://github.com/jamesob/bitcoin.git')
    run('git fetch jamesob')
    if 'martinus' not in remotes:
        run('git remote add martinus https://github.com/martinus/bitcoin.git')
    run('git fetch martinus')

    print(bench_order)
    for ref in bench_order:
        out = _parse_time_output(run_reindex(ref, args['dbcache']))
        print("Finished {}: {}".format(ref, out))
        outd[ref] = out

    return outd


def _parse_time_output(outd):
    return {
        'time': outd['Elapsed (wall clock) time (h:mm:ss or m:ss)'],
        'cpu_perc': outd['Percent of CPU this job got'],
        'mem_kb': int(outd['Maximum resident set size (kbytes)']),
        'user_time_secs': float(outd['User time (seconds)']),
        'system_time_secs': float(outd['System time (seconds)']),
    }


def run_getblocks():
    """Unused."""
    r = run('/usr/bin/time -v ./src/bitcoind -stopatheight=550000 -dbcache=7000 -printtoconsole=0')
    outlines = [i.strip() for i in r.stderr.decode().splitlines()]
    return dict(i.split(': ') for i in outlines)


def run_reindex(ref, dbcache):
    runmany(f"""
        git checkout {ref}
        make clean && make -j $(nproc --ignore=1)
    """)
    run(
        'sync; sudo /sbin/swapoff -a; sudo /sbin/sysctl vm.drop_caches=3; '
        'sudo /usr/local/bin/pyperf system tune; ', check=False,
    )
    r = run(
        f'/usr/bin/time -v ./src/bitcoind -reindex-chainstate -stopatheight=550000 '
        f'-dbcache={dbcache} -connect=0')
    outlines = [i.strip() for i in r.stderr.decode().splitlines()]
    return dict(i.split(': ') for i in outlines)


def install_pyperf():
    runmany("""
        sudo python3.7 -m pip install pyperf
        echo 'ccl     ALL=(ALL) NOPASSWD:/usr/local/bin/pyperf system tune' | sudo tee -a /etc/sudoers
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
                         python_path=['/usr/local/bin/python3.7'],
                         **creds,
                         )

    outd[hostname] = context.call(bench, count, args)
    print('Completed bench on host {}'.format(hostname))
    # context.call(install_pyperf)

    return outd


@mitogen.main()
def main(router):
    parser = argparse.ArgumentParser()
    parser.add_argument('hosts', nargs='+')
    parser.add_argument('--dbcache', type=int, default=4000)
    args = vars(parser.parse_args())

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
    Path(f'bench_reindex.{now}.out').write_text(str(hostname_to_results))
