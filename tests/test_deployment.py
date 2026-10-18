"""Deployment configuration and API entry-point regression tests."""

import pytest

from perf_chat.config import Settings


def test_api_entry_point_uses_environment(monkeypatch, tmp_path):
    import uvicorn

    from perf_chat.api import main

    monkeypatch.setenv("PERF_CHAT_API_HOST", "127.0.0.2")
    monkeypatch.setenv("PERF_CHAT_API_PORT", "8765")
    monkeypatch.setenv("PERF_CHAT_DB", str(tmp_path / "deployment.sqlite3"))
    monkeypatch.setenv("PERF_CHAT_MODE", "demo")
    monkeypatch.setenv("PERF_CHAT_EXECUTOR", "demo")
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    main()
    assert captured == {"host": "127.0.0.2", "port": 8765}
    assert (tmp_path / "deployment.sqlite3").exists()


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_listener_port_is_rejected(port):
    with pytest.raises(ValueError, match="PERF_CHAT_API_PORT"):
        Settings(api_port=port)


def test_empty_listener_host_is_rejected():
    with pytest.raises(ValueError, match="PERF_CHAT_API_HOST"):
        Settings(api_host=" ")
