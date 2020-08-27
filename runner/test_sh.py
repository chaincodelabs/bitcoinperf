import pytest

from . import sh


def test_run():
    ret = sh.run("ls -lah .")

    # returncode
    assert ret.returncode == 0
    assert ret.stderr == ""
    assert ret.stdout

    with pytest.raises(RuntimeError):
        ret = sh.run("cat hopefullynonexistentfile", check=True)

    ret = sh.run("cat hopefullynonexistentfile")

    # returncode
    assert ret.returncode != 0
    assert 'No such file or directory' in ret.stderr
    assert ret.stdout == ""
