"""Offline model API boundary; external e2e tests use explicit credentials."""

import pytest

from test.fixtures.model_api import model_api


@pytest.fixture(scope="session")
def local_model_api():
    with model_api() as api:
        yield api


@pytest.fixture(autouse=True)
def offline_model_credentials(request, monkeypatch):
    if request.node.get_closest_marker("e2e") is None:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
        url, _ = request.getfixturevalue("local_model_api")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-api-key")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", url)
