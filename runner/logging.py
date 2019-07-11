import logging
import logging.handlers
import sys


def configure_logger(cfg: 'Config', log_level: str = 'INFO'):
    logger = get_logger()
    sh = logging.StreamHandler(sys.stdout)
    sh_log_fmt = '%(asctime)s [%(levelname)s] %(message)s'
    sh.setLevel(log_level)
    sh.setFormatter(logging.Formatter(sh_log_fmt))

    # Always log debug out to a file in the workdir
    filehandler = logging.FileHandler(cfg.workdir / "bitcoinperf.log")
    filehandler.setLevel(logging.DEBUG)
    file_log_fmt = '%(asctime)s %(name)s [%(levelname)s] %(message)s'
    filehandler.setFormatter(logging.Formatter(file_log_fmt))

    logger.addHandler(sh)
    logger.addHandler(filehandler)
    logger.setLevel(logging.DEBUG)
    return logger


def get_logger():
    return logging.getLogger('bitcoinperf')
