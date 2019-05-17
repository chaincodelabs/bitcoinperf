import json
import time
import typing as t
import socket
import subprocess
import shutil
from pathlib import Path

from psutil import Process

from . import sh, logging

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


class Node:
    """
    Maintains a subprocess instance pointing to a running bitcoind process,
    provides easy access to the node via RPC.
    """
    # Keep a class-level listing of all created nodes so that we can
    # ensure shutdown.
    all_instances = []

    def __init__(self,
                 bitcoind_bin_path,
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
        self.bitcoind_bin_path = bitcoind_bin_path
        self.bitcoincli_bin_path = bitcoind_bin_path.parent / 'bitcoin-cli'
        self.datadir = datadir
        self.port = port or _find_unused_port()
        self.rpcport = rpcport or _find_unused_port(self.port + 1)
        self.extra_args = extra_args or ''

        self.datadir.mkdir(exist_ok=True)
        if copy_from_datadir:
            shutil.rmtree(self.datadir)
            shutil.copytree(copy_from_datadir, self.datadir)

        self.cmd: sh.Command = None
        # Arguments this node has been started with.
        self.started_args = []

        Node.all_instances.append(self)

    def __repr__(self):
        return "<Node datadir={} port={} rpcport={} pid={}>".format(
            self.datadir, self.port, self.rpcport,
            self.ps.pid if self.ps else None)

    __str__ = __repr__

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
        if 'txindex' in kwargs:
            cmd += '-txindex={} '.format(kwargs.pop('txindex'))
        if 'assumevalid' in kwargs:
            cmd += '-assumevalid={} '.format(kwargs.pop('assumevalid'))
        if 'stopatheight' in kwargs:
            cmd += '-stopatheight={} '.format(kwargs.pop('stopatheight'))
        if 'listen' in kwargs:
            cmd += '-listen={} '.format(kwargs.pop('listen'))
        if 'connect' in kwargs:
            cmd += '-connect={} '.format(kwargs.pop('connect'))
        if 'addnode' in kwargs:
            cmd += '-addnode={} '.format(kwargs.pop('addnode'))

        cmd += '-debug={} '.format(kwargs.pop('debug', 'all'))
        cmd += '{} -port={} -rpcport={}'.format(
            _BENCH_SPECIFIC_BITCOIND_ARGS, self.port, self.rpcport)

        run_cmd = '{} -datadir={} {} {}'.format(
            self.bitcoind_bin_path, self.datadir, self.extra_args, cmd)

        self.start_time = time.time()
        self.cmd = sh.Command(run_cmd, 'run node'.format(self))
        self.cmd.start()
        logger.info("starting node with datadir %s", self.datadir)
        logger.debug("command '%s' starting for %s", run_cmd, self)

    def get_args_dict(self) -> dict:
        """
        Return the performance-relevant arguments this instance was started
        with.
        """
        args = self.cmd.cmd.split('bitcoind')[-1].split()
        args = [a.lstrip('-') for a in args]
        d = {}
        ignore_keys = [
            'connect', 'addnode', 'rpcport', 'datadir', 'port']

        for a in args:
            if any(a.startswith(i) for i in ignore_keys):
                continue
            if '=' in args:
                k, v = args.split('=')
                d[k] = v
            else:
                d[a] = 1

        return d

    def wait_for_init(self, require_height=None) -> int:
        """
        Wait for the node to initialize, return the starting height.

        If require_height is given, ensure that the node starts having a chain
        at least `require_height` high.

        Returns block count.
        """
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
            check_returncode=False)

        # Ignore these lest we spam the logs.
        insignificant_errors = [
            "Rewinding blocks...",
            "Loading block index...",
            "Verifying blocks...",
        ]

        if call[2] != 0:
            if not any(i in call[1].decode() for i in insignificant_errors):
                logger.debug("non-zero returncode from RPC call (%s): %s",
                             self, call)
            return None

        if not deserialize_output:
            logger.debug("rpc: %r -> %r", cmd, call[0])
        else:
            logger.debug("response for %r:\n%s",
                         cmd, json.loads(call[0].decode()))

        return json.loads(call[0].decode()) if deserialize_output else None

    def stop_via_rpc(self):
        logger.info("Calling stop on %s", self)
        self.call_rpc("stop", deserialize_output=False)
        self.ps.wait(timeout=120)

    def terminate(self):
        logger.warning("Terminating %s", self)
        self.ps.terminate()

    def empty_datadir(self):
        """Ensure empty data before each IBD."""
        sh.run("rm -rf %s" % self.datadir, check_returncode=False)
        if not self.datadir.exists():
            self.datadir.mkdir()

    def check_disk_low(self):
        disk_warning_ps = subprocess.run(
            ("tail -n 10000 {}/debug.log | "
             "grep 'Disk space is low!' ").format(self.datadir),
            shell=True)

        # True if we're low on disk
        return disk_warning_ps.returncode == 0

    def check_for_failure(self):
        if self.check_disk_low():
            return True
        return False

    def join(self):
        return self.cmd.join()

    def poll_for_height_and_progress(self) -> \
            t.Tuple[t.Optional[int], t.Optional[float]]:
        """
        Returns the current height and verification progress.

        Returns nothing if the RPC command didn't respond successfully.
        """
        tries_left = 20
        info = None

        while tries_left > 0:
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

        return (last_height_seen, info['verificationprogress'])

    def get_resource_usage(self) -> sh.ResourceUsage:
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


def get_synced_node(cfg, required_height: int) -> t.Optional[Node]:
    """
    Spawns a bitcoind instance that has a synced chain high enough to service
    an IBD up to the last checkpoint (`--ibd-checkpoints`).

    Must be cleaned up by the caller.
    """
    if cfg.synced_peer.address:
        # If we're not running a node locally, don't worry about setup and
        # teardown.
        return None

    server = Node(
        cfg.synced_bitcoin_repo_dir / 'src' / 'bitcoind',
        cfg.synced_datadir,
        extra_args=cfg.synced_bitcoind_args,
    )
    server.start(connect=0, listen=1)
    server.wait_for_init(require_height=required_height)
    logger.info("synced node is active (pid %s)", server.ps.pid)

    return server
