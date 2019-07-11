from . import config, results
from .git import GitCheckout


def test_send_to_codespeed():
    results.CodespeedReporter('', '', '', '').send_to_codespeed(
        GitCheckout('', '', ''), "test_send_to_codespeed", 12, "py.test")
