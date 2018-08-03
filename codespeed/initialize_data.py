from codespeed.models import Project, Environment

Project.objects.get_or_create(
    name='Bitcoin Core',
    defaults=dict(
        repo_type=Project.GIT,
        repo_path='https://github.com/bitcoin/bitcoin',
        commit_browsing_url='https://github.com/bitcoin/bitcoin/commit/{commitid}',
        default_branch='master',
    ))

Environment.objects.get_or_create(
    name='ccl-bench-hdd-1',
    defaults=dict(
        cpu='4x Intel(R) Xeon(R) CPU E3-1220 v5 @ 3.00GHz',
        memory='8GB',
        os='Debian 9.4',
        kernel='4.9.88-1+deb9u1',
    ))
