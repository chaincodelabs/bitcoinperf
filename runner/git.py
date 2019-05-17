import os
import shutil
import typing as t

from . import sh
from .logging import get_logger

logger = get_logger()


GitCheckout = t.NamedTuple('GitCheckout', [
    # e.g. "HEAD"
    ('ref', str),
    # e.g. "e59c59c7befdbb0a600b557f05f009c03f98c2c8"
    ('sha', str),
])

BITCOIN_URL_TEMPLATE = 'https://github.com/{}/bitcoin.git'


def checkout_in_dir(git_path: Path, remote: str, ref: str,
                    copy_from_path: Path = None):
    """Given a path to a repository, cd to it and checkout a specific ref."""
    if not git_path.exists():
        if copy_from_path:
            logger.deubg("Copying bitcoin repo from local path %s", copy_from_path)
            shutil.copytree(copy_from_path, git_path)
        else:
            url = BITCOIN_URL_TEMPLATE.format('bitcoin')
            logger.deubg("Cloning bitcoin repo from url %s", url)
            sh.run("git clone -b {} {} {}".format('master', url, git_path))

    os.chdir(git_path)
    new_remote = target.gitremote
    if new_remote:
        sh.run("git remote add {} https://github.com/{}/bitcoin.git"
            .format(new_remote, new_remote),
            check_returncode=False)
        sh.run("git fetch {}".format(new_remote))

    sh.run("git checkout {}".format(target.gitref))

    gitsha = subprocess.check_output(
        shlex.split('git rev-parse HEAD')).strip().decode()
    co = GitCheckout(sha=gitsha, ref=ref)
    logger.info("Checked out {}".format(co))

    return co
