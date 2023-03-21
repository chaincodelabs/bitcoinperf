import json
import time
import typing as t
import socket
import shutil
import os
import glob
import textwrap
from pathlib import Path

from . import sh, logging, config, git

logger = logging.get_logger()


_BENCH_SPECIFIC_BITCOIND_ARGS = (
    # To "complete" (i.e. latch false out of) initialblockdownload for
    # stopatheight for a lowish height, we need to set a very large maxtipage.
    '-maxtipage=99999999999999999999 '

    # If we don't set minimumchainwork to 0, low heights may cause the syncing
    # peer to never download blocks and thus hang indefinitely during IBD.
    # See https://github.com/bitcoin/bitcoin/blob/e83d82a85c53196aff5b5ac500f20bb2940663fa/src/net_processing.cpp#L517-L521  # noqa
    '-minimumchainwork=0x00 '

    # Output buffering into memory during ps.communicate() can cause OOM errors
    # on machines with small memory, so only output to debug.log files in disk.
    '-printtoconsole=0 '
)

DEFAULT_ASSUMEVALID = (
    '000000000000000000176c192f42ad13ab159fdb20198b87e7ba3c001e47b876')
DEFAULT_DBCACHE = 300


class Node:
    """
    Maintains a subprocess instance pointing to a running bitcoind process,
    provides easy access to the node via RPC.
    """
    # Keep a class-level listing of all created nodes so that we can
    # ensure shutdown.
    all_instances: t.List['Node'] = []

    def __init__(self,
                 repo_path,
                 datadir,
                 copy_from_datadir: Path = None,
                 port: int = None,
                 rpcport: int = None,
                 extra_args: str = None,
                 ):
        """
        Kwargs:
            copy_from_datadir: if specified, initialize the datadir contents of
                this node from the specified path.

            If port and rpcport are left unspecified, unused ports will be
                found and used automatically.
        """
        self.repo_path = repo_path
        self.bitcoincli_bin_path = repo_path / 'src' / 'bitcoin-cli'
        self.datadir = datadir
        self.port = port or _find_unused_port()
        self.rpcport = rpcport or _find_unused_port(self.port + 1)
        self.extra_args = extra_args or ''

        if copy_from_datadir:
            if self.datadir.exists():
                sh.rm(self.datadir)
            logger.info(
                f'Seeding datadir from {copy_from_datadir} -> {self.datadir}')
            shutil.copytree(copy_from_datadir, self.datadir)
        else:
            self.datadir.mkdir(exist_ok=True)

            # Make sure to clear out any old debug.log we've inherited from the
            # source datadir so that our log start time isn't screwed up
            # (see logparse.get_log_start()).
            if (self.datadir / 'debug.log').exists():
                os.unlink(str(self.datadir / 'debug.log'))

        self.cmd: t.Optional[sh.Command] = None
        # Arguments this node has been started with.
        self.started_args: t.List[dict] = []

        Node.all_instances.append(self)

    def __repr__(self):
        return "<Node datadir={} port={} rpcport={} pid={}>".format(
            self.datadir, self.port, self.rpcport,
            self.ps.pid if self.ps else None)

    __str__ = __repr__

    def checkout_and_build(self, target: config.Target):
        pass

    @property
    def is_process_alive(self):
        self.ps.poll()
        return self.ps and self.ps.returncode is None

    @property
    def ps(self):
        if not self.cmd or not self.cmd.ps:
            return None
        return self.cmd.ps

    def start(self, **kwargs):
        self.started_args.append(dict(kwargs))
        cmd = ''

        if 'dbcache' in kwargs:
            cmd += '-dbcache={} '.format(kwargs.pop('dbcache'))
        elif 'dbcache' not in self.extra_args:
            cmd += '-dbcache={} '.format(DEFAULT_DBCACHE)

        # Supply a default assumevalid value unless the user has overridden it
        # at some point.
        if 'assumevalid' in kwargs:
            cmd += '-assumevalid={} '.format(kwargs.pop('assumevalid'))
        elif 'assumevalid' not in self.extra_args:
            cmd += '-assumevalid={} '.format(DEFAULT_ASSUMEVALID)

        if kwargs.get('debug'):
            cmd += '-debug={} '.format(kwargs['debug'])
        else:
            cmd += '-debug=coindb -debug=bench '

        # Add remaining arguments
        for k, v in kwargs.items():
            cmd += '-{}={} '.format(k, v)

        cmd += '{} -port={} -rpcport={}'.format(
            _BENCH_SPECIFIC_BITCOIND_ARGS, self.port, self.rpcport)

        run_cmd = '{} -datadir={} {} {}'.format(
            self.repo_path / 'src' / 'bitcoind',
            self.datadir, self.extra_args, cmd)

        self.start_time = time.time()
        self.cmd = sh.Command(run_cmd, 'run node {}'.format(self))
        self.cmd.start()
        logger.debug("command '%s' starting for %s", run_cmd, self)

    def get_args_dict(self) -> t.Dict[str, str]:
        """
        Return the performance-relevant arguments this instance was started
        with.
        """
        assert self.cmd
        args = self.cmd.cmd.split('bitcoind')[-1].split()
        args = [a.lstrip('-') for a in args]
        d = {}
        ignore_keys = [
            'connect', 'addnode', 'rpcport', 'datadir', 'port']

        for a in args:
            if any(a.startswith(i) for i in ignore_keys):
                continue
            if '=' in a:
                k, v = a.split('=')
                d[k] = v
            else:
                d[a] = '1'

        return d

    def wait_for_init(self, require_height=None) -> t.Optional[int]:
        """
        Wait for the node to initialize, return the starting height.

        If require_height is given, ensure that the node starts having a chain
        at least `require_height` high.

        Returns block count, if node successfully started.
        """
        assert self.cmd
        num_tries = 600
        sleep_time_secs = 1
        bitcoind_up = False
        block_count = None

        while num_tries > 0 and self.is_process_alive and not bitcoind_up:
            info = self.call_rpc("getblockchaininfo")

            if info and require_height and info["blocks"] < require_height:
                # Stop process; we're exiting.
                self.stop_via_rpc()
                raise RuntimeError(
                    "bitcoind node doesn't have enough blocks (%s vs. %s)" %
                    (info['blocks'], require_height))
            elif info:
                bitcoind_up = True
                block_count = int(info['blocks'])
            else:
                num_tries -= 1
                time.sleep(sleep_time_secs)

        if not self.is_process_alive:
            self.cmd.join()
            raise RuntimeError(
                "Node process died (code {})\nCommand:\n{}\n\n"
                "stdout:\n\n{}\n\nstderr:\n\n{}\n".format(
                    self.cmd.returncode,
                    self.cmd.cmd,
                    self.cmd.stderr,
                    self.cmd.stdout))

        if not bitcoind_up:
            raise RuntimeError(
                "Couldn't bring node up: {} with command {}".format(
                    self, self.cmd.cmd))

        logger.info("Node %s initialized successfully", self)
        return block_count

    def call_rpc(self, cmd,
                 deserialize_output=True,
                 quiet=False,
                 ) -> t.Optional[dict]:
        """
        Call some bitcoin RPC command and return its deserialized output.
        """
        call = sh.run(
            "{} -rpcport={} -datadir={} {}".format(
                self.bitcoincli_bin_path, self.rpcport, self.datadir, cmd),
            check=False)

        # Ignore these lest we spam the logs.
        insignificant_errors = [
            "Rewinding blocks...",
            "Loading block index...",
            "Verifying blocks...",
        ]

        if call.returncode != 0:
            if not any(i in call.stderr for i in insignificant_errors):
                logger.debug("non-zero returncode from RPC call (%s): %s",
                             self, call)
            return None

        if not deserialize_output:
            logger.debug("rpc: %r -> %r", cmd, call.stdout)
        else:
            logger.debug("response for %r:\n%s", cmd, json.loads(call.stdout))

        return json.loads(call.stdout) if deserialize_output else None

    def stop_via_rpc(self, timeout=None):
        logger.info("Calling stop on %s", self)
        self.call_rpc("stop", deserialize_output=False)
        self.cmd.join(timeout=timeout)

    def terminate(self):
        logger.warning("Terminating %s", self)
        self.ps.terminate()

    def empty_datadir(self):
        """Ensure empty data before each IBD."""
        sh.run("rm -rf %s" % self.datadir, check=False)
        if not self.datadir.exists():
            self.datadir.mkdir()

    def check_disk_low(self):
        disk_warning_ps = sh.run(
            ("tail -n 10000 {}/debug.log | "
             "grep 'Disk space is low!' ").format(self.datadir))

        # True if we're low on disk
        return disk_warning_ps.returncode == 0

    def join(self, timeout=None):
        return self.cmd.join(timeout=timeout)

    def poll_for_height_and_progress(self) -> \
            t.Tuple[t.Optional[int], t.Optional[float]]:
        """
        Returns the current height and verification progress.

        Returns nothing if the RPC command didn't respond successfully.
        """
        tries_left = 20
        info = None

        while tries_left > 0 and not info:
            info = self.call_rpc("getblockchaininfo")

            if not info:
                tries_left -= 1
                time.sleep(1)

        if not info:
            logger.error(
                "Bitcoind hasn't responded to RPC in a suspiciously "
                "long time... hung?")
            return (None, None)

        last_height_seen = info['blocks']
        logger.debug("[%s] saw height %s", self, last_height_seen)

        return (int(last_height_seen), float(info['verificationprogress']))

    def get_resource_usage(self) -> sh.ResourceUsage:
        assert self.cmd
        return self.cmd.get_resource_usage()


def _find_unused_port(startval=8888) -> int:
    """Return an unused port."""
    portbad = True
    portnum = startval

    while portbad:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", portnum))
        except socket.error:
            portnum += 1
        else:
            portbad = False
        finally:
            s.close()

    return portnum


# Synced peers should only be built once per process, since their git ref
# will never change within a bitcoinperf process. Cache them here.
_built_peer_cache: t.Dict[config.SyncedPeer, bool] = {}


def get_synced_node(
    cfg: config.Config,
    peer_config: config.SyncedPeer,
    required_height: t.Optional[int] = None,
    target: t.Optional[config.Target] = None,
) -> t.Optional[Node]:
    """
    Spawns a bitcoind instance that has a synced chain high enough to service
    an IBD up to the last checkpoint (`--ibd-checkpoints`).

    Must be cleaned up by the caller.
    """
    curr_path = Path.cwd()

    if peer_config.address:
        # If we're not running a node locally, don't worry about setup and
        # teardown.
        return None

    if peer_config.gitref and not _built_peer_cache.get(peer_config):
        logger.info(f'Starting build for synced peer ({peer_config})')
        target = target or config.Target(gitref=peer_config.gitref, rebase=False)
        assert peer_config.repodir
        [co], _ = git.resolve_targets(peer_config.repodir, [target])
        git.checkout_in_dir(peer_config.repodir, target)
        builder = BuildManager(
            peer_config.repodir.parent,
            repo_path=peer_config.repodir,
            cache_path=cfg.build_cache_path(),
            clean=False)
        cmd = builder.build(target, config.Compilers.gcc)

        if cmd and cmd.returncode != 0:  # i.e. if build was not cached
            raise RuntimeError(
                f"{target} (synced peer) failed to build with gcc "
                f"({peer_config})")

        logger.info(f'Finished build for synced peer ({peer_config})')

        _built_peer_cache[peer_config] = True

    server = Node(
        peer_config.repodir,
        peer_config.datadir,
        extra_args=peer_config.bitcoind_extra_args,
    )
    server.start(connect=0, listen=1)
    server.wait_for_init(require_height=required_height)
    logger.info("synced node is active (pid %s)", server.ps.pid)

    # Clean up any path changes.
    sh.cd(curr_path)
    return server


class BuildManager:

    def __init__(self,
                 workdir: Path,
                 cache_path: t.Optional[Path] = None,
                 clean: bool = True,
                 repo_path: Path = None):
        """
        Args:
            cache_path: if given, cache builds at this location.
            clean: should run `make distclean`?
        """
        self.workdir = workdir
        self.cache_path = cache_path
        self.clean = clean
        self.repo_path = repo_path or self.workdir / 'bitcoin'

    def build(self,
              target: config.Target,
              compiler: config.Compilers,
              *,
              num_jobs: t.Optional[int] = None,
              copy_log_to: t.Optional[Path] = None
              ) -> t.Optional[sh.Command]:
        """
        Checks out the bitcoin repo to the desired target and builds
        bitcoind.

        DESTRUCTIVE: this will change pwd to the bitcoin dir.

        Pre-call assumptions:
          - The repo has been checked out at `self.repo_path`

        Returns: a completed Command if we did a build, None if we used cache.
        """
        cache_key = target.cache_key(compiler)
        logger.info(f"Starting build for {target.id} (cache key: {cache_key})")
        makefile = self.repo_path / 'Makefile'
        sh.cd(self.repo_path)

        git.checkout_in_dir(self.repo_path, target)

        # Sanity check - compare the commit message as an extra assurance.
        msg = git.get_commit_msg('HEAD')
        assert target.gitco
        if msg != target.gitco.commit_msg:
            raise RuntimeError(
                "checkout {} has bad HEAD (expected '{}', got '{}')!".format(
                    self.repo_path, target.gitco.commit_msg, msg))

        num_jobs = num_jobs or config.DEFAULT_NPROC

        cache = BuildCache(self.workdir, compiler, self.cache_path)
        if self.cache_path and cache.restore(target):
            return None

        if makefile.exists() and self.clean:
            logger.info('Running make clean')
            sh.run('make clean')

        if not (self.repo_path / 'configure').exists():
            logger.info("Running autogen.sh")
            assert sh.run("./autogen.sh").ok

        configure_prefix = ''
        if compiler == config.Compilers.clang:
            configure_prefix = 'CC=clang CXX=clang++ '

        # Ensure build is clean.
        makefile_path = self.repo_path / 'Makefile'
        if makefile_path.is_file() and self.clean:
            sh.run('make distclean')

        boostflags = ''
        armlib_path = '/usr/lib/arm-linux-gnueabihf/'

        if Path(armlib_path).is_dir():
            # On some architectures we need to manually specify this,
            # otherwise configuring with clang can fail.
            boostflags = '--with-boost-libdir=%s' % armlib_path

        logger.info("Running ./configure ...")
        conf = sh.run(
            configure_prefix +
            './configure --with-incompatible-bdb ' +
            '--without-gui ' +  # TODO maybe make this configurable?
            target.configure_args +
            # Ensure ccache is disabled so that subsequent make runs
            # are timed accurately.
            '--disable-ccache ' + boostflags)

        if not conf.ok:
            logger.error(conf.failure_msg(f"configure failed for {target}"))
            if copy_log_to:
                sh.run(f'cp config.log {copy_log_to}/config.log')
                logger.info("Saved configure output to %s", copy_log_to)
            raise RuntimeError('configure failed')

        logger.info(f"Running make -j {num_jobs}")
        cmd = sh.Command(f"make -j {num_jobs}")
        cmd.start()
        cmd.join()

        if copy_log_to:
            (copy_log_to / 'make.stdout').write_bytes(cmd.stdout or b'')
            (copy_log_to / 'make.stderr').write_bytes(cmd.stderr or b'')
            logger.info("Saved make output to %s", copy_log_to)

        if cmd.returncode != 0:
            # make failed; compilation error?
            comp_err = textwrap.indent('\n'.join(cmd.stderr_lines), '  ')
            logger.warning(f"make failed; compilation error:\n{comp_err}")
            return cmd

        _assert_version(self.repo_path, target.gitco)

        if cmd.returncode == 0 and self.cache_path:
            cache.save(target)
            cache.clean()
        elif self.cache_path:
            logger.warning(
                "Unable to save build %s to cache-path %s", target, self.cache_path)

        # cmd error will be handled by caller
        return cmd


class BuildCache:
    """
    Utility for caching built bitcoin binaries. This allows us to switch back
    and forth between benchmark targets without having to rebuild.
    """
    def __init__(self, workdir: Path, compiler: config.Compilers, cachedir: Path = None):
        self.workdir = workdir
        self.repo_path = workdir / 'bitcoin'
        self.cachedir = cachedir or (workdir / 'build-cache')
        self.cachedir.mkdir(exist_ok=True)

        # The compiler used affects the cache key
        self.compiler = compiler

    def _get_cache_path(self, target: config.Target):
        return (self.cachedir / target.cache_key(self.compiler)).resolve()

    def save(self, target: config.Target):
        cache = self._get_cache_path(target)
        logger.info("Copying build to cache %s", cache)
        starttime = time.time()
        shutil.copytree(self.repo_path, cache)
        logger.info("Cached build %s in %.2fs", cache, time.time() - starttime)

    def restore(self, target: config.Target) -> bool:
        """
        Restore the build cache from a previous run.

        Pre-call assumptions:
          - The repo has been checked out at `self.repo_path`

        Returns True if we restored the build from cache.
        """
        assert target.gitco
        cache = self._get_cache_path(target)

        if not cache.exists():
            return False

        logger.info(
            "Cached version of build %s found - "
            "restoring from that and skipping build ", target.cache_key(self.compiler))

        sh.cd(self.workdir)
        sh.rm(self.repo_path)
        shutil.copytree(cache, self.repo_path)
        _assert_version(self.repo_path, target.gitco)
        sh.cd(self.repo_path)
        return True

    def clean(self):
        files_in_cache = glob.glob("{}/*".format(self.cachedir))
        files_in_cache.sort(key=os.path.getmtime, reverse=True)

        # TODO parameterize
        CACHE_SIZE = 5

        # reverse=True above because we only want to delete if we're over
        # the cache size.
        for stale in files_in_cache[CACHE_SIZE:]:
            logger.info("Deleting stale cache %s", stale)
            sh.rm(Path(stale))


def _assert_version(repodir: Path, gitco: config.GitCheckout):
    """Ensure we've checked out a specific version of bitcoin."""
    srcdir = repodir / 'src'
    # Sanity check - compare version as reported by binary
    for bin in (srcdir / 'bitcoind', srcdir / 'bitcoin-cli'):
        version_line = sh.run(f'{bin} -version | head -n 1').stdout
        sha = gitco.sha
        ref = gitco.ref
        version = version_line.split('version ')[-1]

        if sha[:7] not in version and ref not in version:
            msg = f'expected: {sha} (or {ref}) \nsaw: {version_line}'
            raise RuntimeError(f'bad checkout: {repodir}\n{msg}')
