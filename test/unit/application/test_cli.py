"""CLI entry: deterministic simulator subcommand behaviour."""

import pytest

from gateway.cli import main


def test_simulate_prints_deterministic_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate", "--prompt", "hello", "--model", "m"]) == 0
    assert capsys.readouterr().out == "[mock:m] hello\n"


def test_simulate_stream_reassembles_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate", "--prompt", "hello", "--stream"]) == 0
    assert capsys.readouterr().out == "[mock:mock-model] hello\n"


def test_simulate_includes_context_items(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate", "--prompt", "hi", "--context", "a", "--context", "b"]) == 0
    assert capsys.readouterr().out == "[mock:mock-model] hi (context: a | b)\n"


def test_simulate_requires_prompt() -> None:
    with pytest.raises(SystemExit):
        main(["simulate"])


def test_missing_command_is_an_error() -> None:
    with pytest.raises(SystemExit):
        main([])
