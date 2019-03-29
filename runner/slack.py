import requests
import json
import logging
import logging.handlers

from . import config
from .globals import G_, GitCheckout


class Client:
    def __init__(self, webhook_url=None):
        self.webhook_url = webhook_url

    def send_to_slack_txt(self, cfg, txt):
        self._send_to_slack({'text': "[%s] %s" % (config.HOSTNAME, txt)})

    def send_to_slack_attachment(
            self, gitco: GitCheckout, title, fields, text="", success=True):
        fields['Host'] = config.HOSTNAME
        fields['Commit'] = (gitco.sha or '')[:6]
        fields['Branch'] = gitco.branch

        data = {
            "attachments": [{
                "title": title,
                "fields": [
                    {"title": title, "value": val, "short": True}
                    for (title, val) in fields.items()
                ],
                "color": "good" if success else "danger",
            }],
        }

        if text:
            data['attachments'][0]['text'] = text

        self._send_to_slack(data)

    def _send_to_slack(self, slack_data):
        if not self.webhook_url:
            return

        response = requests.post(
            self.webhook_url, data=json.dumps(slack_data),
            headers={'Content-Type': 'application/json'}
        )
        if response.status_code != 200:
            raise ValueError(
                'Request to slack returned an error %s, the response is:\n%s'
                % (response.status_code, response.text)
            )


class SlackLogHandler(logging.Handler):
    def __init__(self, client: Client):
        self.client = client
        super().__init__()

    def emit(self, record):
        fmtd = self.format(record)

        # If the log is multiple lines, treat the first line as the title and
        # the remainder as text.
        title, *rest = fmtd.split('\n', 1)
        return self.client.send_to_slack_attachment(
            G_.gitco, title, {},
            text=(rest[0] if rest else None), success=False)


def attach_slack_handler_to_logger(client: Client, logger):
    """Can't do this in .logging because we need a cfg argument."""
    slack = SlackLogHandler(client)
    slack.setLevel(logging.WARNING)
    slack.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(slack)
