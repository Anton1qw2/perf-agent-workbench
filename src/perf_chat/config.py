import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/perf-chat.sqlite3"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    mode: str = "demo"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    timeout: float = 90
    executor_mode: str = "demo"
    profiler_mcp_url: str = ""
    profiler_mcp_timeout: float = 60
    git_repositories: dict[str, str] = field(default_factory=dict)
    model_input_bytes: int = 120_000

    def __post_init__(self):
        if not self.api_host.strip():
            raise ValueError("PERF_CHAT_API_HOST must not be empty")
        if not 1 <= self.api_port <= 65535:
            raise ValueError("PERF_CHAT_API_PORT must be between 1 and 65535")
        if not isinstance(self.git_repositories, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in self.git_repositories.items()
        ):
            raise ValueError(
                "PERF_CHAT_GIT_REPOSITORIES must map repository aliases to local paths"
            )
        if not 4096 <= self.model_input_bytes <= 1_000_000:
            raise ValueError("PERF_CHAT_MODEL_INPUT_BYTES must be between 4096 and 1000000")
        for name, timeout in [
            ("LLM_TIMEOUT_SECONDS", self.timeout),
            ("PROFILER_MCP_TIMEOUT_SECONDS", self.profiler_mcp_timeout),
        ]:
            if not math.isfinite(timeout) or not 0 < timeout <= 120:
                raise ValueError(f"{name} must be finite and between 0 and 120 seconds")

    @classmethod
    def from_env(cls):
        load_dotenv()
        value = cls(
            db_path=os.getenv("PERF_CHAT_DB", "data/perf-chat.sqlite3"),
            api_host=os.getenv("PERF_CHAT_API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("PERF_CHAT_API_PORT", "8000")),
            mode=os.getenv("PERF_CHAT_MODE", "demo"),
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", ""),
            model=os.getenv("LLM_MODEL", ""),
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
            executor_mode=os.getenv("PERF_CHAT_EXECUTOR", "demo"),
            profiler_mcp_url=os.getenv("PROFILER_MCP_URL", ""),
            profiler_mcp_timeout=float(os.getenv("PROFILER_MCP_TIMEOUT_SECONDS", "60")),
            git_repositories=json.loads(os.getenv("PERF_CHAT_GIT_REPOSITORIES", "{}")),
            model_input_bytes=int(os.getenv("PERF_CHAT_MODEL_INPUT_BYTES", "120000")),
        )
        if value.mode not in {"demo", "llm"}:
            raise ValueError("PERF_CHAT_MODE must be demo or llm")
        if value.mode == "llm" and not value.model:
            raise ValueError("Set LLM_MODEL for llm mode")
        if value.executor_mode not in {"demo", "profile-demo", "profile-mcp"}:
            raise ValueError("PERF_CHAT_EXECUTOR must be demo, profile-demo or profile-mcp")
        if value.executor_mode == "profile-mcp" and not value.profiler_mcp_url:
            raise ValueError("Set PROFILER_MCP_URL for profile-mcp execution")
        Path(value.db_path).parent.mkdir(parents=True, exist_ok=True)
        return value
