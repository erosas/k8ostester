from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_context():
    """(k8s, driver, events, namespace) — the Worker constructor arguments."""
    return MagicMock(), MagicMock(), MagicMock(), "test-ns"