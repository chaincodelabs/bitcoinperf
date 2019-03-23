import os
from pathlib import Path

import pytest

from . import sh, config


def test_get_peak_mem():
    if Path("/proc").is_dir():
        assert sh.get_proc_peakmem_kib(os.getpid()) is not None


def test_run():
    cfg = config.parse_args("")
    (stdout, stderr, code) = sh.run("ls -lah .")

    # returncode
    assert code == 0
    assert stderr.decode() == ""
    assert stdout.decode()

    with pytest.raises(RuntimeError):
        (stdout, stderr, code) = sh.run("cat hopefullynonexistentfile")

    (stdout, stderr, code) = sh.run(
        "cat hopefullynonexistentfile",
        check_returncode=False,
    )

    # returncode
    assert code != 0
    assert stderr.decode()
    assert stdout.decode() == ""
