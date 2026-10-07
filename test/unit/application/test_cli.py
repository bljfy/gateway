"""CLI entry: deterministic simulator subcommand behaviour."""

import pytest

from gateway.cli import build_parser, main


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


@pytest.mark.parametrize("command", ["client", "demo"])
def test_prompt_sources_are_mutually_exclusive(command: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "--prompt", "hello", "--prompt-file", "input.txt"])


def test_client_requires_prompt_source() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["client"])


@pytest.mark.parametrize("command", ["client", "gateway", "simulator"])
def test_role_config_defaults(command: str) -> None:
    args = [command, "--prompt", "hello"] if command == "client" else [command]
    assert build_parser().parse_args(args).config.as_posix() == f".tools/demo/{command}.json"


def test_init_demo_defaults() -> None:
    args = build_parser().parse_args(["init-demo"])
    assert args.directory.as_posix() == ".tools/demo"
    assert args.manifest.as_posix() == ".tools/gmssl/manifest.json"
