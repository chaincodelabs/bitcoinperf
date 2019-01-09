from . import endpoints, config


def test_send_to_codespeed():
    cfg = config.parse_args("")
    cfg.run_data.gitsha = "f" * 32
    cfg.run_data.gitref = "test"

    endpoints.send_to_codespeed(cfg, "test_send_to_codespeed", 12, "py.test")
