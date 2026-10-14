"""CLI entry: adapter metadata, prompt sources and command defaults."""

import asyncio
import io
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gateway.cli import build_parser, main
from test.fixtures.adapter_output import response_body


def test_infer_prints_deterministic_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["infer", "--prompt", "hello", "--model", "m"]) == 0
    assert response_body(capsys.readouterr().out) == "[mock:m] hello\n"


def test_infer_stream_reassembles_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["infer", "--prompt", "hello", "--stream"]) == 0
    assert response_body(capsys.readouterr().out) == "[mock:deepseek-flash] hello\n"


def test_infer_includes_context_items(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["infer", "--prompt", "hi", "--context", "a", "--context", "b"]) == 0
    assert response_body(capsys.readouterr().out) == "[mock:deepseek-flash] hi (context: a | b)\n"


def test_infer_requires_prompt() -> None:
    with pytest.raises(SystemExit):
        main(["infer"])


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


@pytest.mark.parametrize("command", ["client", "gateway", "adapter"])
def test_role_config_defaults(command: str) -> None:
    args = [command, "--prompt", "hello"] if command == "client" else [command]
    assert build_parser().parse_args(args).config.as_posix() == f".tools/demo/{command}.json"


def test_init_demo_defaults() -> None:
    args = build_parser().parse_args(["init-demo"])
    assert args.directory.as_posix() == ".tools/demo"
    assert args.manifest.as_posix() == ".tools/gmssl/manifest.json"


def test_infer_uses_explicit_request_uuid(capsys: pytest.CaptureFixture[str]) -> None:
    request_id = uuid4()
    assert main(["infer", "--prompt", "hello", "--request-id", str(request_id)]) == 0
    assert response_body(capsys.readouterr().out, request_id) == "[mock:deepseek-flash] hello\n"


class FlushedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushed_values = []

    def flush(self):
        self.flushed_values.append(self.getvalue())


def test_infer_flushes_each_stream_chunk(monkeypatch):
    output = FlushedOutput()
    monkeypatch.setattr("gateway.cli.sys.stdout", output)
    assert main(["infer", "--prompt", "hello", "--stream"]) == 0
    assert any(value.endswith("hello") for value in output.flushed_values)
    assert output.flushed_values[-1].endswith("hello\n")


@pytest.mark.parametrize("command", ["client", "demo"])
def test_secure_cli_flushes_stream_output(command, monkeypatch, tmp_path):
    output = FlushedOutput()
    monkeypatch.setattr("gateway.cli.sys.stdout", output)

    async def close():
        pass

    runtime = SimpleNamespace(
        gateway=SimpleNamespace(log_level="INFO"), manager=SimpleNamespace(close=close)
    )
    monkeypatch.setattr("gateway.cli.load_runtime", lambda *_: runtime)

    async def server(_runtime, *, ready, stop=None):
        ready()
        if stop is None:
            await asyncio.Event().wait()

    async def client(_runtime, _request, *, stream, write):
        assert stream
        write("first")
        assert output.flushed_values == ["first"]
        write("second")
        assert output.flushed_values == ["first", "firstsecond"]

    monkeypatch.setattr("gateway.cli.run_server", server)
    monkeypatch.setattr("gateway.cli.run_client", client)
    args = [command, "--prompt", "synthetic", "--stream"]
    if command == "demo":
        args += ["--directory", str(tmp_path)]
    assert main(args) == 0
