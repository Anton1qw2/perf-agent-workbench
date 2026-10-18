import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.integrations.profiler_mcp import (
    DemoProfilerGateway,
    MCPProfilerGateway,
    decode_tool_result,
)
from perf_chat.profiler_analysis_agent import AnalysisRequest, ProfileAnalysisAgent
from perf_chat.profiler_analysis_agent.executor import ProfileAnalysisExecutor
from perf_chat.store import Conflict, Store
from perf_chat.worker import Worker


def request(**kwargs):
    return AnalysisRequest(result_id="result-7", goal="Find CPU hotspots", **kwargs)


class Model:
    def __init__(self, invalid=False):
        self.payload = None
        self.invalid = invalid

    def complete(self, system, payload, **kwargs):
        self.payload = payload
        return json.dumps(
            {
                "observations": [
                    {
                        "text": "The server reports 120 CPU samples for demo_function.",
                        "evidence_ids": ["nonexistent" if self.invalid else "e2"],
                    }
                ],
                "hypotheses": [],
                "recommendations": ["Inspect this function."],
            }
        )


def test_graph_synthetic_data_and_optional_assembly():
    model = Model()
    report = ProfileAnalysisAgent(DemoProfilerGateway(), model).analyze(request(include_asm=True))
    assert report.status == "completed" and report.demo
    assert [e.operation for e in report.evidence] == [
        "get_list_metrics",
        "get_hotspots",
        "get_asm",
    ]
    assert all(e.sha256 and e.retrieved_at for e in report.evidence)
    assert report.documentation["status"] == "empty"
    assert model.payload["demo"] is True
    assert report.observations[0].evidence_ids == ["e2"]


def test_no_model_returns_evidence_without_invented_analysis():
    report = ProfileAnalysisAgent(DemoProfilerGateway()).analyze(request())
    assert report.status == "completed"
    assert len(report.evidence) == 2
    assert report.observations == []
    assert any("disabled" in item for item in report.limitations)


def test_unknown_metric_requires_input_and_does_not_query():
    class Gateway(DemoProfilerGateway):
        def get_hotspots(self, *args, **kwargs):
            pytest.fail("Must not query an unknown metric")

    report = ProfileAnalysisAgent(Gateway()).analyze(request(metrics=["unknown"]))
    assert report.status == "needs_input"
    assert report.open_questions and len(report.evidence) == 1


def test_rejects_unknown_evidence_and_retains_raw_results():
    report = ProfileAnalysisAgent(DemoProfilerGateway(), Model(invalid=True)).analyze(request())
    assert report.status == "insufficient_data"
    assert report.observations == []
    assert len(report.evidence) == 2


def test_partial_mcp_failure_retains_prior_evidence():
    class Gateway(DemoProfilerGateway):
        def get_asm(self, *args, **kwargs):
            raise RuntimeError("sensitive server response")

    report = ProfileAnalysisAgent(Gateway()).analyze(request(include_asm=True))
    assert report.status == "insufficient_data"
    assert len(report.evidence) == 2
    assert "sensitive" not in report.model_dump_json()


def test_adapter_uses_exact_mcp_tool_names_and_arguments():
    calls = []

    def caller(name, args):
        calls.append((name, args))
        return ["cycles"] if name == "get_list_metrics" else {"cycles": 17}

    agent = ProfileAnalysisAgent(MCPProfilerGateway("unused", caller=caller))
    report = agent.analyze(request(include_asm=True))
    assert report.demo is False
    assert calls == [
        ("get_list_metrics", {"result_id": "result-7"}),
        ("get_hotspots", {"result_id": "result-7", "metrics": ["cycles"]}),
        ("get_asm", {"result_id": "result-7", "metrics": ["cycles"]}),
    ]


def test_mcp_tool_result_decoding_and_error():
    def result(**kwargs):
        return SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[
                SimpleNamespace(type="text", text='["cycles"]'),
            ],
            **kwargs,
        )

    assert decode_tool_result(result()) == ["cycles"]
    bad = result()
    bad.isError = True
    with pytest.raises(ValueError, match="reported an error"):
        decode_tool_result(bad)


def test_executor_clarifies_result_id_and_completes():
    executor = ProfileAnalysisExecutor(ProfileAnalysisAgent(DemoProfilerGateway()))
    task = PerformanceTask(summary="Analyze CPU")
    _, cursor = executor.step("run", task, {})
    event, cursor = executor.step("run", task, cursor)
    assert event.type == "clarification"
    event, cursor = executor.step(
        "run",
        task,
        cursor,
        ClarificationAnswer(
            request_id=event.clarification.request_id,
            answer="result-7",
        ),
    )
    assert event.result.status == "demo"
    assert "SYNTHETIC DEMO" in event.result.summary
    assert cursor["analysis"]["request"]["result_id"] == "result-7"


def test_profile_worker_persists_analysis_in_run(tmp_path):
    settings = Settings(db_path=str(tmp_path / "state.db"), executor_mode="profile-demo")
    store = Store(settings.db_path)
    worker = Worker(settings)
    cid = store.create_conversation()["id"]
    conv = store.conversation(cid)
    spec = conv["spec"]
    spec.update(summary="Inspect CPU hotspots")
    spec["target"].update(repository="local/repo", branch="main", profile="result-7")
    store.update_spec(cid, PerformanceTask.model_validate(spec), spec["spec_version"])
    conv = store.conversation(cid)
    rid = uuid4().hex
    rid = store.start_run(cid, conv["spec"]["spec_version"], rid)["id"]
    assert worker.tick() and worker.tick()
    run = store.run(rid)
    assert run["status"] == "demo"
    assert run["cursor"]["analysis"]["evidence"][1]["operation"] == "get_hotspots"


def test_metric_selection_can_be_retried_and_resumed():
    class Gateway(DemoProfilerGateway):
        def get_list_metrics(self, result_id):
            return [f"metric-{i}" for i in range(7)]

    executor = ProfileAnalysisExecutor(ProfileAnalysisAgent(Gateway()))
    task = PerformanceTask(summary="Analyze", target={"profile": "result-7"})
    _, cursor = executor.step("run", task, {})
    event, cursor = executor.step("run", task, cursor)
    assert event.type == "clarification"
    assert cursor["analysis"]["status"] == "needs_input"
    answer = ClarificationAnswer(request_id=event.clarification.request_id, answer="not JSON")
    event, cursor = executor.step("run", task, cursor, answer)
    assert event.type == "clarification"
    answer.answer = '["metric-2"]'
    event, cursor = executor.step("run", task, cursor, answer)
    assert event.type == "result"
    assert cursor["analysis"]["evidence"][1]["arguments"]["metrics"] == ["metric-2"]


def test_unknown_catalog_is_not_guessed():
    gateway = MCPProfilerGateway("unused", caller=lambda *args: {"undocumented": "cycles"})
    report = ProfileAnalysisAgent(gateway).analyze(request())
    assert report.status == "insufficient_data"
    assert len(report.evidence) == 1


def test_mcp_timeout_becomes_report_limitation():
    import asyncio

    gateway = MCPProfilerGateway("unused", timeout=0.001)

    async def blocked(*args):
        await asyncio.sleep(10)

    gateway._invoke = blocked
    report = ProfileAnalysisAgent(gateway).analyze(request())
    assert report.status == "insufficient_data"
    assert report.evidence == []


def test_oversized_evidence_is_retained_without_model_call():
    class Gateway(DemoProfilerGateway):
        def get_hotspots(self, *args, **kwargs):
            return {"text": "x" * 130_000}

    model = Model()
    report = ProfileAnalysisAgent(Gateway(), model).analyze(request())
    assert report.status == "insufficient_data"
    assert model.payload is None
    assert len(report.evidence) == 2
    assert any("too large" in limitation for limitation in report.limitations)


def test_current_profile_and_goal_override_stale_details():
    executor = ProfileAnalysisExecutor(ProfileAnalysisAgent(DemoProfilerGateway()))
    task = PerformanceTask(
        summary="Current goal",
        target={"profile": "current-id"},
        details={
            "profile_analysis": {"result_id": "stale-id", "goal": "Stale goal"},
        },
    )
    _, cursor = executor.step("run", task, {})
    _, cursor = executor.step("run", task, cursor)
    assert cursor["analysis"]["request"]["result_id"] == "current-id"
    assert cursor["analysis"]["request"]["goal"] == "Current goal"


def test_new_clarification_rejects_late_answer_to_previous_question(tmp_path):
    class Gateway(DemoProfilerGateway):
        def get_list_metrics(self, result_id):
            return [f"m{i}" for i in range(7)]

    settings = Settings(db_path=str(tmp_path / "clarification.db"))
    store = Store(settings.db_path)
    worker = Worker(settings, executor=ProfileAnalysisExecutor(ProfileAnalysisAgent(Gateway())))
    cid = store.create_conversation()["id"]
    task = PerformanceTask(
        summary="Inspect",
        target={
            "repository": "repo",
            "branch": "main",
            "profile": "id",
        },
    )
    store.update_spec(cid, task, 1)
    rid = store.start_run(cid, 2, uuid4().hex)["id"]
    worker.tick()
    worker.tick()
    first = store.run(rid)["clarification"]["request_id"]
    stale = ClarificationAnswer(request_id=first, answer="invalid JSON")
    store.resume(rid, stale)
    worker.tick()
    second = store.run(rid)["clarification"]["request_id"]
    assert first != second
    with pytest.raises(Conflict, match="different clarification"):
        store.resume(rid, stale)


def test_expired_analysis_does_not_start_more_calls_or_succeed(monkeypatch, tmp_path):
    import time

    now = [time.monotonic()]
    monkeypatch.setattr("perf_chat.runtime.time.monotonic", lambda: now[0])

    class Gateway(DemoProfilerGateway):
        def get_hotspots(self, *args, **kwargs):
            now[0] += 2
            return super().get_hotspots(*args, **kwargs)

        def get_asm(self, *args, **kwargs):
            pytest.fail("Must not start another call after the deadline")

    settings = Settings(db_path=str(tmp_path / "budget.db"))
    store = Store(settings.db_path)
    worker = Worker(settings, executor=ProfileAnalysisExecutor(ProfileAnalysisAgent(Gateway())))
    cid = store.create_conversation()["id"]
    task = PerformanceTask(
        summary="Inspect",
        target={
            "repository": "repo",
            "branch": "main",
            "profile": "id",
        },
        budget={"max_runtime_seconds": 1},
        details={"profile_analysis": {"include_asm": True}},
    )
    store.update_spec(cid, task, 1)
    rid = store.start_run(cid, 2, uuid4().hex)["id"]
    worker.tick()
    worker.tick()
    run = store.run(rid)
    assert run["status"] == "failed"
    assert "budget exhausted" in run["result"]["summary"]
    assert run["cursor"]["analysis"]["evidence"][-1]["operation"] == "get_hotspots"


@pytest.mark.parametrize("target", [
    {"profile": "result-7"},
    {"profile": "result-7", "repository": "local/repo"},
    {"profile": "result-7", "branch": "main"},
])
@pytest.mark.parametrize("policy", ["off", "optional", "required"])
def test_missing_source_binding_does_not_trap_single_input_run(target, policy):
    executor = ProfileAnalysisExecutor(ProfileAnalysisAgent(DemoProfilerGateway()))
    task = PerformanceTask(
        summary="Analyze CPU", target=target,
        details={"profile_analysis": {"source": {"policy": policy}}},
    )
    snapshot = task.model_dump()
    assert task.missing_fields() == []
    _, cursor = executor.step("single-input", task, {})
    event, cursor = executor.step("single-input", task, cursor)
    if policy == "required":
        assert event.type == "clarification"
        assert event.clarification.fields == ["source"]
        assert "start a new run" in event.clarification.question
        event, cursor = executor.step(
            "single-input", task, cursor,
            ClarificationAnswer(
                request_id=event.clarification.request_id, answer='{"policy":"off"}',
            ),
        )
    assert event.type == "result"
    assert event.result.status == "demo"
    assert cursor["analysis"]["request"]["source"] is None
    if policy == "optional":
        assert any("Optional source enrichment skipped" in item for item in event.result.limitations)
    assert task.model_dump() == snapshot
