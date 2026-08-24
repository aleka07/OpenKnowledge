import os

import pytest


def pytest_collection_modifyitems(config, items):
    """Integration tests need the live server DB — skipped unless KB_IT=1.

    Run everything on gx10-1 with:  KB_IT=1 uv run pytest
    Run pure unit tests anywhere:   uv run pytest
    """
    if os.environ.get("KB_IT") == "1":
        return
    skip = pytest.mark.skip(reason="integration test: set KB_IT=1 (needs live DB)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs the live Postgres on gx10-1")
