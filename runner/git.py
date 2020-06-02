import os
import shutil
import typing as t
from pathlib import Path

from . import sh, config
from .logging import get_logger
from .util import is_hex
from .config import GitCheckout

logger = get_logger()


BITCOIN_URL_TEMPLATE = 'https://github.com/{}/bitcoin.git'


def git_cache_path():
    p = config.config_path / 'bitcoin.cached.git'
    p.mkdir(exist_ok=True, parents=True)
    return p


def cache_repo():
    """Make an initial clone of the bitcoin repo to the bitcoinperf-wide cache.

    The resulting repo will be used to copy from for faster "cloning."
    """
    cache_path = git_cache_path()

    if cache_path.exists() and (cache_path / 'autogen.sh').exists():
        return True
    elif cache_path.exists():
        # Invalid repo?
        sh.rm(cache_path)

    url = BITCOIN_URL_TEMPLATE.format('bitcoin')
    logger.info("Cloning bitcoin repo from url %s", url)
    sh.run("git clone {} {}".format(url, cache_path))

    sh.cd(cache_path)

    sh.run('git fetch')
    sh.run("git checkout origin/master")


def get_repo(git_path: Path, cached_okay: bool = True):
    """
    Check out the bitcoin git repo to a path if necessary, optionally
    copying from a cache.
    """
    copy_from_path = None

    if cached_okay:
        cache_repo()
    if cached_okay and git_cache_path().exists():
        copy_from_path = git_cache_path()

    if not git_path.exists():
        if copy_from_path:
            logger.info(
                "Copying bitcoin repo from local path %s", copy_from_path)
            shutil.copytree(copy_from_path, git_path)
        else:
            url = BITCOIN_URL_TEMPLATE.format('bitcoin')
            logger.info("Cloning bitcoin repo from url %s", url)
            sh.run("git clone {} {}".format(url, git_path))

    sh.cd(git_path)
    sh.run('git fetch --all')
    sh.run("git checkout origin/master")


def checkout_in_dir(git_path: Path, target: config.Target) -> GitCheckout:
    """
    Given a path to a repository, cd to it and checkout a specific ref.

    Incoming targets should have been fully resolved by calling
    `resolve_targets()` beforehand - they should have valid `.gitco` objects
    attached.
    """
    assert target.gitco, 'Target must be resolved before checking out.'
    co = target.gitco
    if sh.run(f"git checkout {co.sha}").returncode != 0:
        raise RuntimeError(f"sha {co.sha} was not valid in {git_path}")
    logger.info("Checked out {}".format(co))
    return co


# When given, find the merge-base in origin/master relative to the other
# target.
MERGEBASE_REF = '$mergebase'


def resolve_targets(repo_path: Path,
                    targets: t.List[config.Target]
                    ) -> t.Tuple[t.List[GitCheckout], t.List[config.Target]]:
    """
    Intake targets and resolve each to a specific commit hash.

    Capable of resolving a special ref `MERGEBASE_REF` relative to another
    target. Also handles rebasing the target commit if requested.

    This should be called once before any checking out is done.

    DESTRUCTIVE: attaches the new checkout objects to the targets passed in
        (`Target.gitco`).

    Returns:
        (list of checkouts, list of targets that failed to check out).
    """
    if not repo_path.exists():
        get_repo(repo_path)

    sh.cd(repo_path)

    remotes = {
        i.split()[0] for i in
        sh.run('git remote -v').stdout.splitlines()}

    def get_remote(name):
        if name == 'origin':
            return
        if name not in remotes:
            url = BITCOIN_URL_TEMPLATE.format(name)
            sh.run(f'git remote add {name} {url}')
        sh.run(f'git fetch --force {name} --tags')

    if 'origin' not in remotes:
        sh.run('git remote add origin https://github.com/bitcoin/bitcoin.git')

    sh.run("git fetch origin --force --tags")

    # Clear any local modifications.
    sh.run("git reset --hard origin/master")

    for remote in {tar.gitremote for tar in targets}:
        get_remote(remote)

    mergebase = [t for t in targets if MERGEBASE_REF in [t.name, t.gitref]]
    others = [t for t in targets if MERGEBASE_REF not in [t.name, t.gitref]]

    for tar in others:
        if not tar.gitref.startswith('pr/'):
            continue

        num = tar.gitref.split('pr/')[-1]
        sh.run(f"git fetch --force {tar.gitremote} "
               f"pull/{num}/head:refs/remotes/{tar.gitremote}/pr/{num}")

    bad_targets = []
    checkouts = []

    # Special case to infer merge-base target if one exists.
    if mergebase:
        mergebase_target = mergebase[0]
        if len(others) != 1:
            raise ValueError(
                "can only process mergebase against one other target")

        other = others[0]
        mergebase_sha = get_git_mergebase(
            repo_path, other.gitremote, other.gitref)

        co = GitCheckout(
            ref=mergebase_sha,
            remote='origin',
            sha=mergebase_sha,
            commit_msg=get_commit_msg(mergebase_sha),
            name=f'origin/master (merge-base)',
        )
        checkouts.append(co)
        mergebase_target.gitco = co

    # Resolve all of the non-mergebase targets into GitCheckouts.
    for tar in others:
        ishex = is_hex(tar.gitref)
        bad = False
        msg = ''
        sha = ''
        pre_rebase_sha = None

        if ishex:
            sha = tar.gitref
            bad = sh.run(f'git show {tar.gitref}').returncode != 0
        else:
            sha_res = sh.run(f'git rev-parse {tar.gitremote}/{tar.gitref}')
            if not sha_res.ok:
                # Fall back to just trying the ref, no remote. Sometimes for
                # tags this is necessary.
                sha_res = sh.run(f'git rev-parse {tar.gitref}')
                if not sha_res.ok:
                    bad = True

            if sha_res.ok:
                sha = sha_res.stdout.strip()

        if bad:
            logger.warning(f'ref not found: {tar.gitref}')
            bad_targets.append(tar)
            continue

        # Handle requested rebase.
        if tar.rebase:
            sh.run("git config user.email 'bench@bitcoinperf.com'")
            sh.run("git config user.name 'Bitcoinperf'")
            sh.run('git rebase origin/master')
            logger.info("Rebased %s (%s) on top of origin/master (%s)",
                        tar.gitref, sha, get_sha('origin/master'))
            pre_rebase_sha = sha
            sha = get_sha('HEAD')

        msg = get_commit_msg(sha)
        co = GitCheckout(
            ref=tar.gitref,
            remote=tar.gitremote,
            sha=sha,
            commit_msg=msg,
            name=(tar.name or ''),
            pre_rebase_sha=pre_rebase_sha,
        )
        checkouts.append(co)
        tar.gitco = co

    return checkouts, bad_targets


def get_sha(ref: str) -> str:
    return sh.run(f'git rev-parse {ref}').stdout.strip()


def get_commit_msg(ref: str) -> str:
    return sh.run(f'git log -1 --pretty=%B {ref}').stdout.strip()


def get_git_mergebase(repo_path: Path, remote: str, name: str) -> str:
    sh.cd(repo_path)

    arg = f'{remote}/{name}' if not is_hex(name) else name

    base = sh.run(f'git merge-base origin/master {arg}')
    if not base.ok:
        raise ValueError(f"could not get merge-base for {arg}")

    return base.stdout.strip()
