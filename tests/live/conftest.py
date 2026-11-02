import os

import pytest

from retail_agent.config import Settings, load_env
from retail_agent.query.executor import make_executor


@pytest.fixture(scope="session")
def settings() -> Settings:
    load_env()
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        pytest.skip("GOOGLE_CLOUD_PROJECT is not set")
    return Settings()


@pytest.fixture(scope="session")
def executor(settings):
    return make_executor(settings)
