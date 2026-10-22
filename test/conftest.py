"""Offline model API boundary; external e2e tests use explicit credentials."""

import importlib.util
import os
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from test.fixtures.model_api import model_api

E2E_SECRETS = pytest.StashKey[tuple[str, ...]]()


@pytest.fixture
def e2e_environment(request, monkeypatch):
    """Prepare real-service tests without changing the invoking shell's environment."""
    monkeypatch.setattr(request.config.option, "tbstyle", "short")
    monkeypatch.setattr(request.config.option, "showlocals", False)
    monkeypatch.setattr(request.config.option, "showcapture", "no")
    for name, value in dotenv_values(request.config.rootpath / ".env", interpolate=False).items():
        if value is not None and name not in os.environ:
            monkeypatch.setenv(name, value)
    request.node.stash[E2E_SECRETS] = tuple(
        value
        for name, value in os.environ.items()
        if value
        and any(part in name.upper() for part in ("API_KEY", "PASSWORD", "TOKEN", "SECRET"))
    )
    # HTTPX creates transports for ALL_PROXY even when HTTPS_PROXY takes priority.
    # Ignore an unsupported, redundant SOCKS transport only when HTTPS has a proxy.
    https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY", "")
    if (
        urlsplit(https_proxy).scheme in {"http", "https"}
        and importlib.util.find_spec("socksio") is None
    ):
        for name in ("ALL_PROXY", "all_proxy"):
            if urlsplit(os.environ.get(name, "")).scheme in {"socks5", "socks5h"}:
                monkeypatch.delenv(name, raising=False)
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        pytest.fail(
            "E2E requires DEEPSEEK_API_KEY in the environment or project .env", pytrace=False
        )
    if urlsplit(os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).scheme != "https":
        pytest.fail(
            "E2E requires an HTTPS model service, not a local HTTP substitute", pytrace=False
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if item.get_closest_marker("e2e") is None:
        return
    report = outcome.get_result()
    secrets = item.stash.get(E2E_SECRETS, ())

    def redact(value):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
            value = value.replace(repr(secret)[1:-1], "[REDACTED]")
        return value

    if report.longrepr:
        report.longrepr = redact(report.longreprtext)
        if call.excinfo:
            # Preserve diagnostic categories, not raw SDK messages or request data.
            exception = call.excinfo.value
            categories = []
            seen = set()
            while exception is not None and id(exception) not in seen:
                seen.add(id(exception))
                category = type(exception).__name__
                status = getattr(exception, "status_code", None)
                if isinstance(status, int):
                    category += f" (HTTP {status})"
                categories.append(category)
                exception = exception.__cause__ or exception.__context__
            report.longrepr += "\nE2E exception categories: " + " -> ".join(categories)
    # Do not attach captured SDK or business output to E2E reports.
    report.sections = []


@pytest.fixture(scope="session")
def local_model_api():
    with model_api() as api:
        yield api


@pytest.fixture(autouse=True)
def offline_model_credentials(request, monkeypatch):
    if request.node.get_closest_marker("e2e") is not None:
        request.getfixturevalue("e2e_environment")
    else:
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
