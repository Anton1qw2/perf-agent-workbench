import json
import os
import sys

import pytest
from dotenv import load_dotenv

from perf_chat.profiler_analysis_agent.__main__ import main


@pytest.fixture
def configure_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("perf_chat.config.load_dotenv", lambda: load_dotenv(tmp_path / ".env"))
    for key in list(os.environ):
        if key.startswith(("PERF_CHAT_", "PROFILER_MCP_", "GIT_MCP_", "LLM_")):
            monkeypatch.delenv(key)

    def configure(executor):
        (tmp_path / ".env").write_text(
            f"PERF_CHAT_MODE=demo\nPERF_CHAT_EXECUTOR={executor}\n"
            "PROFILER_MCP_URL=https://profiler.example/mcp\n", encoding="utf-8",
        )
    return configure


def test_standalone_demo_comes_from_dotenv_without_cli_flag(configure_environment, monkeypatch, capsys):
    configure_environment("profile-demo")
    monkeypatch.setattr(sys, "argv", ["analyzer", "--result-id", "r7", "--goal", "Inspect"])
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["demo"] is True
    assert "SYNTHETIC DEMO" in report["chat_report"]["content"]


@pytest.mark.parametrize("executor, flags", [("profile-mcp", ["--demo"]), ("demo", [])])
def test_standalone_rejects_cli_mode_override_or_non_analyzer_executor(
    configure_environment, monkeypatch, capsys, executor, flags,
):
    configure_environment(executor)
    monkeypatch.setattr(sys, "argv", ["analyzer", "--result-id", "r7", "--goal", "Inspect", *flags])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "PERF_CHAT_EXECUTOR" in capsys.readouterr().err
