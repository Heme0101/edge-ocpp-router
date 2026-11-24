import dataclasses
from datetime import datetime, timezone

import pytest

from config import load_site_config

# Site time zone is America/Los_Angeles; in November that is PST (UTC-8).
OFF_PEAK = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)    # 10:00 local
PEAK = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)         # 17:00 local


@pytest.fixture
def cfg():
    """The real site_config.json, with the session log switched off for tests."""
    return dataclasses.replace(load_site_config(), session_log_path=None)
