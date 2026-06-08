"""Shared test fixtures. The whole suite is offline and fast."""

import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="session")
def gold():
    """metafor / escalc gold values (see tests/fixtures/metafor_gold.json)."""
    with open(os.path.join(FIXTURES, "metafor_gold.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def sample_csv():
    """Path to the bundled real Cochrane trial-level sample (CD000028)."""
    return os.path.join(FIXTURES, "sample_pairwise70.csv")


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """A throwaway BEAST_HOME for end-to-end CLI/store tests."""
    home = tmp_path / "beast_home"
    monkeypatch.setenv("BEAST_HOME", str(home))
    return str(home)
