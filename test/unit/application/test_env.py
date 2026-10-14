"""CLI dotenv loading preserves process settings and literal credential values."""

import os

import pytest

from gateway.cli import main


@pytest.mark.parametrize("command", ["adapter", "demo", "infer", "init-demo"])
def test_model_commands_load_local_env(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=synthetic-${KEEP_LITERAL}\n")
    monkeypatch.setenv("KEEP_LITERAL", "must-not-expand")

    def check(_args):
        assert os.environ["DEEPSEEK_API_KEY"] == "synthetic-${KEEP_LITERAL}"
        return 0

    callback = {"adapter": "_server", "demo": "_demo", "infer": "_infer", "init-demo": "_init_demo"}
    monkeypatch.setattr("gateway.cli." + callback[command], check)
    args = [command, "--prompt", "synthetic"] if command == "infer" else [command]
    assert main(args) == 0


def test_process_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=file-value\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-value")

    def check(_args):
        assert os.environ["DEEPSEEK_API_KEY"] == "process-value"
        return 0

    monkeypatch.setattr("gateway.cli._infer", check)
    assert main(["infer", "--prompt", "synthetic"]) == 0


@pytest.mark.parametrize("command", ["gateway", "client"])
def test_other_roles_do_not_load_model_env(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=file-value\n")

    def check(_args):
        assert "DEEPSEEK_API_KEY" not in os.environ
        return 0

    monkeypatch.setattr("gateway.cli." + ("_server" if command == "gateway" else "_client"), check)
    args = [command, "--prompt", "synthetic"] if command == "client" else [command]
    assert main(args) == 0
