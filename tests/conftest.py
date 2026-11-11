import pytest

from app.config import load_settings
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.runner import StandInThread


@pytest.fixture
def standin():
    """standin(profile, scale, **profile_changes) -> (provider, app, base_url)"""
    threads = []

    def start(profile="normal", scale=0.05, **changes):
        st = StandInThread(profile, seed=0, scale=scale, **changes).start()
        threads.append(st)
        settings = load_settings(standin_base_url=st.api_base)
        return OpenAICompatProvider.standin(settings), st.app, st.base

    yield start
    for st in threads:
        st.stop()
