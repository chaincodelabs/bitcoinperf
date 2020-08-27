from pathlib import Path

import pytest


@pytest.fixture
def datadir(request):
    return Path(request.fspath.dirname) / 'test_data'
