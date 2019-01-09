import logging
import logging.handlers
import sys

from . import endpoints


class SlackLogHandler(logging.Handler):
    def emit(self, record):
        fmtd = self.format(record)

        # If the log is multiple lines, treat the first line as the title and
        # the remainder as text.
        title, *rest = fmtd.split('\n', 1)
        return endpoints.send_to_slack_attachment(
            title, {}, text=(rest[0] if rest else None), success=False)


def get_logger(log_level='INFO'):
    logger = logging.getLogger(__name__)
    sh = logging.StreamHandler(sys.stdout)
    log_fmt = '%(asctime)s %(name)s [%(levelname)s] %(message)s'
    sh.setLevel(log_level)
    sh.setFormatter(logging.Formatter(log_fmt))

    filehandler = logging.handlers.TimedRotatingFileHandler(
        "bitcoinperf.log", when='D', interval=2)
    filehandler.setLevel(logging.DEBUG)
    filehandler.setFormatter(logging.Formatter(log_fmt))

    slack = SlackLogHandler()
    slack.setLevel(logging.WARNING)
    slack.setFormatter(logging.Formatter('%(message)s'))

    logger.addHandler(sh)
    logger.addHandler(filehandler)
    logger.addHandler(slack)
    logger.setLevel(logging.DEBUG)
    return logger
