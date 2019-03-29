"""
Globals that are (reluctantly) referenced and modified throughout runtime
of a benchmark.
"""
import typing as t
from argparse import Namespace

G_ = Namespace()


GitCheckout = t.NamedTuple('GitCheckout', [
    # e.g. "master"
    ('branch', str),
    # e.g. "HEAD"
    ('ref', str),
    # e.g. "e59c59c7befdbb0a600b557f05f009c03f98c2c8"
    ('sha', str),
])

# The git checkout currently being benched.
G_.gitco = GitCheckout('', '', '')

# The compiler currently in use.
G_.compiler = None

# The directory we populate with the code being benchmarked. Contains a
# `bitcoin/` folder.
G_.workdir = None

# Did we acquire the system-wide lockfile?
G_.lockfile_acquired = False
