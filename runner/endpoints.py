import requests
import json
import logging

from . import config

logger = logging.getLogger('bitcoinperf')


def send_to_codespeed(
        cfg,
        bench_name, result, executable,
        lessisbetter=True, units_title='Time', units='seconds', description='',
        result_max=None, result_min=None, extra_data=None):
    """
    Send a benchmark result to codespeed over HTTP.
    """
    # Mandatory fields
    data = {
        'commitid': cfg.run_data.gitsha,
        'branch': cfg.repo_branch,
        'project': 'Bitcoin Core',
        'executable': executable,
        'benchmark': bench_name,
        'environment': cfg.codespeed_envname,
        'result_value': result,
        # Optional. Default is taken either from VCS integration or from
        # current date
        # 'revision_date': current_date,
        # 'result_date': current_date,  # Optional, default is current date
        # 'std_dev': std_dev,  # Optional. Default is blank
        'max': result_max,  # Optional. Default is blank
        'min': result_min,  # Optional. Default is blank
        # Ignored if bench_name already exists:
        'lessisbetter': lessisbetter,
        'units_title': units_title,
        'units': units,
        'description': description,
        'extra_data': extra_data or {},
    }

    logger.debug(
        "Attempting to send benchmark (%s, %s) to codespeed",
        bench_name, result)

    if not cfg.codespeed_url:
        return

    resp = requests.post(
        cfg.codespeed_url + '/result/add/',
        data=data, auth=(cfg.codespeed_user, cfg.codespeed_password))

    if resp.status_code != 202:
        raise ValueError(
            'Request to codespeed returned an error %s, the response is:\n%s'
            % (resp.status_code, resp.text)
        )


def send_to_slack_txt(cfg, txt):
    _send_to_slack(cfg, {'text': "[%s] %s" % (config.HOSTNAME, txt)})


def send_to_slack_attachment(cfg, title, fields, text="", success=True):
    fields['Host'] = config.HOSTNAME
    fields['Commit'] = (cfg.run_data.gitsha or '')[:6]
    fields['Branch'] = cfg.repo_branch

    data = {
        "attachments": [{
            "title": title,
            "fields": [
                {"title": title, "value": val, "short": True} for (title, val)
                in fields.items()
            ],
            "color": "good" if success else "danger",
        }],
    }

    if text:
        data['attachments'][0]['text'] = text

    _send_to_slack(cfg, data)


def _send_to_slack(cfg, slack_data):
    if not cfg.slack_webhook_url:
        return

    response = requests.post(
        cfg.slack_webhook_url, data=json.dumps(slack_data),
        headers={'Content-Type': 'application/json'}
    )
    if response.status_code != 200:
        raise ValueError(
            'Request to slack returned an error %s, the response is:\n%s'
            % (response.status_code, response.text)
        )
