import logging
import logging.handlers
import sys


def configure_logger(log_level='INFO'):
    logger = get_logger()
    sh = logging.StreamHandler(sys.stdout)
    log_fmt = '%(asctime)s %(name)s [%(levelname)s] %(message)s'
    sh.setLevel(log_level)
    sh.setFormatter(logging.Formatter(log_fmt))

    filehandler = logging.handlers.TimedRotatingFileHandler(
        "bitcoinperf.log", when='D', interval=2)
    filehandler.setLevel(logging.DEBUG)
    filehandler.setFormatter(logging.Formatter(log_fmt))

    logger.addHandler(sh)
    logger.addHandler(filehandler)
    logger.setLevel(logging.DEBUG)
    return logger


def get_logger():
    return logging.getLogger('bitcoinperf')
