import json
import time
from uuid import uuid4

import pytest

from perf_chat.config import Settings
from perf_chat.contracts import PerformanceTask
from perf_chat.integrations.git_mcp import MCPGitGateway
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent import (
    AnalysisRequest,
    AnalysisStep,
    DynamicProfilerAnalyzer,
    ProfileAnalysis,
)
from perf_chat.worker import Worker


def catalog():
    return [{"name": "inspect", "description": "Read repository data.",
             "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}},
                              "required": ["repo"], "additionalProperties": False}}]


def git_gateway(calls, *, allowed=None, tools=None, response=None):
    return MCPGitGateway(
        "https://git.example/mcp", token="synthetic-env-token",
        allowed_tools=["inspect"] if allowed is None else allowed,
        tool_lister=lambda: catalog() if tools is None else tools,
        caller=lambda name, args: calls.append((name, args)) or (
            {"revision": "server-reported-revision", "text": "int hot();"}
            if response is None else response
        ),
    )


class Model:
    def __init__(self, decisions=None):
        self.decisions = list(decisions or [
            {"action": "call", "server": "git", "tool": "inspect",
             "arguments": {"repo": "team/project"}, "reason": "Inspect repository"},
            {"action": "finish", "reason": "Enough source context"},
        ])
        self.payloads = []

    def complete(self, system, payload, **kwargs):
        self.payloads.append(payload)
        if payload["schema"]["title"] == "ToolDecision":
            return json.dumps(self.decisions.pop(0))
        return json.dumps({"observations": [
            {"text": "The repository server returned source reference data.",
             "evidence_ids": [item["id"]]} for item in payload["evidence"]
        ]})


def analyzer(git, model=None, profiler=None):
    return DynamicProfilerAnalyzer(profiler or MCPProfilerGateway(""), model or Model(),
                                   git_gateway=git)


def request(**kwargs):
    return AnalysisRequest(repository="team/project", goal="Inspect source", **kwargs)


def test_repository_only_analysis_uses_git_evidence_and_never_exposes_token():
    calls, model = [], Model()
    report = analyzer(git_gateway(calls), model).analyze(request())
    assert report.status == "completed" and not report.demo
    assert calls == [("inspect", {"repo": "team/project"})]
    assert report.request.result_id == ""
    assert report.evidence[0].server == "git"
    assert report.git_tool_catalog[0]["name"] == "inspect"
    assert report.tool_catalog == []
    assert "git: inspect" in report.chat_report.content
    assert any("immutable commit/blob identity" in note for note in report.limitations)
    assert "synthetic-env-token" not in json.dumps([report.model_dump(), model.payloads])


def test_equal_tool_names_on_two_servers_route_independently():
    git_calls, profiler_calls = [], []
    profiler = MCPProfilerGateway(
        "https://profiler.example/mcp", allowed_tools=["inspect"], tool_lister=catalog,
        caller=lambda name, args: profiler_calls.append((name, args)) or {"samples": 2},
    )
    decisions = [
        {"action": "call", "server": server, "tool": "inspect",
         "arguments": {"repo": "team/project"}, "reason": "Inspect"}
        for server in ["profiler", "git"]
    ] + [{"action": "finish", "reason": "Enough evidence"}]
    model = Model(decisions)
    report = analyzer(git_gateway(git_calls), model, profiler).analyze(request(result_id="r7"))
    assert report.status == "completed"
    assert len(git_calls) == len(profiler_calls) == 1
    assert [item.server for item in report.evidence] == ["profiler", "git"]
    assert len({item.id for item in report.evidence}) == 2
    assert {item["server"] for item in model.payloads[0]["tools"]} == {"profiler", "git"}


def test_git_allowlist_is_empty_by_default_and_disallowed_calls_never_execute():
    calls = []
    gateway = git_gateway(calls, allowed=[])
    report = analyzer(gateway).analyze(request())
    assert report.status == "insufficient_data" and not calls
    with pytest.raises(ValueError, match="not allowed"):
        gateway.call_tool(gateway.list_tools()[0], {"repo": "team/project"})


def test_git_policy_and_schema_drift_between_plan_and_call_fail_closed():
    calls, tools = [], catalog()
    gateway = git_gateway(calls, tools=tools)
    agent = analyzer(gateway)
    first = agent.step_analysis(request(), checkpoint={})
    planned = agent.step_analysis(request(), checkpoint=first.checkpoint)
    assert planned.checkpoint["phase"] == "call"
    changed = analyzer(MCPGitGateway("https://other.example/mcp", allowed_tools=["inspect"]))
    report = changed.step_analysis(request(), checkpoint=planned.checkpoint).report
    assert report.status == "insufficient_data" and "policy changed" in report.limitations[-1]
    tools[0]["description"] = "Changed definition"
    report = agent.step_analysis(request(), checkpoint=planned.checkpoint).report
    assert report.status == "insufficient_data" and not calls


def test_git_source_off_prevents_discovery_and_calls():
    gateway = git_gateway([])
    gateway.list_tools = lambda: pytest.fail("Disabled Git server must not be discovered")
    report = analyzer(gateway).analyze(request(source={
        "repository": "team/project", "ref": "HEAD", "policy": "off",
    }))
    assert report.status == "insufficient_data" and report.git_tool_catalog == []


def test_git_invalid_arguments_can_be_repaired_without_a_failed_call():
    calls = []
    model = Model([
        {"action": "call", "server": "git", "tool": "inspect",
         "arguments": {"repo": 7}, "reason": "Read source"},
        {"action": "call", "server": "git", "tool": "inspect",
         "arguments": {"repo": "team/project"}, "reason": "Repair arguments"},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    report = analyzer(git_gateway(calls), model).analyze(request())
    assert report.status == "completed" and len(calls) == 1
    assert model.payloads[1]["argument_feedback"]["server"] == "git"


def test_worker_restores_git_only_run_without_requesting_a_profile(tmp_path, monkeypatch):
    calls, model = [], Model()
    monkeypatch.setattr("perf_chat.worker.ChatModel", lambda settings: model)
    monkeypatch.setattr("perf_chat.worker.MCPGitGateway", lambda *a, **kw: git_gateway(calls))
    settings = Settings(db_path=str(tmp_path / "git.db"), executor_mode="profile-mcp",
                        mode="llm", model="test", git_mcp_url="https://git.example/mcp")
    worker = Worker(settings)
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(summary="Inspect source",
                                                target={"repository": "team/project"}), 1)
    rid = worker.store.start_run(cid, 2, uuid4().hex)["id"]
    for _ in range(20):
        worker = Worker(settings)
        worker.tick()
        run = worker.store.run(rid)
        assert run["status"] != "waiting"
        if run["status"] in {"succeeded", "failed"}:
            break
    assert run["status"] == "succeeded" and len(calls) == 1
    assert run["cursor"]["analysis"]["evidence"][0]["server"] == "git"
    assert "synthetic-env-token" not in json.dumps(run)


def test_git_deadline_excludes_late_success():
    calls = []
    gateway = git_gateway(calls)
    agent = analyzer(gateway)
    discovered = agent.step_analysis(request(), checkpoint={})
    planned = agent.step_analysis(request(), checkpoint=discovered.checkpoint)

    def late_response(name, args):
        calls.append((name, args))
        time.sleep(0.15)
        return {"text": "Late source response"}

    gateway.caller = late_response
    report = agent.step_analysis(request(), checkpoint=planned.checkpoint,
                                 deadline=time.monotonic() + 0.1).report
    assert report.status == "insufficient_data" and len(calls) == 1
    assert report.evidence == []


@pytest.mark.parametrize("url", ["http://remote.example/mcp", "", "https://user:pass@git.example/mcp"])
def test_git_env_token_rejects_insecure_destinations_without_echoing(url):
    with pytest.raises(ValueError) as error:
        Settings(git_mcp_url=url, git_mcp_token="synthetic-env-token")
    assert "synthetic-env-token" not in str(error.value)


def test_git_env_configuration_and_token_transport(monkeypatch, tmp_path):
    monkeypatch.setenv("PERF_CHAT_MODE", "demo")
    monkeypatch.setenv("PERF_CHAT_EXECUTOR", "profile-mcp")
    monkeypatch.setenv("PERF_CHAT_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("PROFILER_MCP_URL", "")
    monkeypatch.setenv("GIT_MCP_URL", "http://127.0.0.1:9000/mcp")
    monkeypatch.setenv("GIT_MCP_ALLOWED_TOOLS", '["inspect"]')
    monkeypatch.setenv("GIT_MCP_TOKEN", "synthetic-env-token")
    settings = Settings.from_env()
    assert settings.git_allowed_tools == ["inspect"] and not settings.profiler_mcp_url
    assert "synthetic-env-token" not in repr(settings)
    gateway = MCPGitGateway(settings.git_mcp_url, token=settings.git_mcp_token)
    assert gateway.auth_headers() == {"Authorization": "Bearer synthetic-env-token"}
    gateway.url = "http://remote.example/mcp"
    with pytest.raises(ValueError):
        gateway.auth_headers()


@pytest.mark.parametrize("options", [
    {"git_allowed_tools": "inspect"}, {"git_allowed_tools": [""]},
    {"git_mcp_timeout": 0}, {"git_mcp_timeout": float("inf")},
    {"git_mcp_token": "synthetic\nsecret", "git_mcp_url": "https://git.example/mcp"},
])
def test_git_configuration_rejects_invalid_limits_and_token_format(options):
    with pytest.raises(ValueError):
        Settings(**options)


def test_git_only_custom_analyzer_does_not_need_builtin_gateway_attributes(tmp_path):
    received = []

    class CustomAnalyzer:
        analyzer_id = "custom-repository-graph-v1"

        def step_analysis(self, request, **kwargs):
            received.append(request)
            return AnalysisStep(report=ProfileAnalysis(
                status="completed", request=request, recommendations=["Custom graph completed."],
            ))

    def factory(services):
        assert isinstance(services.git, MCPGitGateway)
        return CustomAnalyzer()

    settings = Settings(db_path=str(tmp_path / "custom.db"), executor_mode="profile-mcp",
                        git_mcp_url="https://git.example/mcp")
    worker = Worker(settings, analyzer_factory=factory)
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(summary="Inspect repository",
                                                target={"repository": "team/project"}), 1)
    rid = worker.store.start_run(cid, 2, uuid4().hex)["id"]
    for _ in range(10):
        worker.tick()
        run = worker.store.run(rid)
        assert run["status"] != "waiting"
        if run["status"] in {"succeeded", "failed"}:
            break
    assert run["status"] == "succeeded" and received[0].result_id == ""
