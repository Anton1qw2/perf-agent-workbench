from types import SimpleNamespace
from uuid import uuid4

import pytest
from analysis_helpers import PlanningModel, advance, analysis_agent, drain_worker

from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.integrations.profiler_mcp import DemoProfilerGateway, decode_tool_result
from perf_chat.profiler_analysis_agent import AnalysisRequest, DynamicProfilerAnalyzer
from perf_chat.profiler_analysis_agent.executor import ProfileAnalysisExecutor
from perf_chat.store import Conflict
from perf_chat.worker import Worker


def test_single_analyzer_handles_synthetic_evidence_and_interpretation():
    report = analysis_agent().analyze(AnalysisRequest(result_id="r7", goal="Inspect"))
    assert report.status == "completed" and report.demo
    assert len(report.evidence) == 1
    assert report.evidence[0].sha256 and report.evidence[0].retrieved_at
    assert report.observations[0].evidence_ids == ["e1"]
    assert report.documentation["status"] == "empty"


def test_real_analysis_without_model_does_not_guess_calls():
    report = DynamicProfilerAnalyzer(DemoProfilerGateway()).analyze(
        AnalysisRequest(result_id="r7", goal="Inspect"),
    )
    assert report.status == "insufficient_data" and report.tool_catalog
    assert report.evidence == []


def test_mcp_text_decoding_and_error_handling():
    result = SimpleNamespace(isError=False, structuredContent=None,
                             content=[SimpleNamespace(type="text", text='["cycles"]')])
    assert decode_tool_result(result) == ["cycles"]
    result.isError = True
    with pytest.raises(ValueError, match="reported an error"):
        decode_tool_result(result)


def test_executor_clarifies_result_id_then_drives_durable_steps():
    executor = ProfileAnalysisExecutor(analysis_agent())
    task = PerformanceTask(summary="Inspect")
    event, cursor = advance(executor, "run", task, {})
    assert event.clarification.fields == ["result_id"]
    event, cursor = advance(executor, "run", task, cursor, ClarificationAnswer(
        request_id=event.clarification.request_id, answer="r7",
    ))
    assert event.result.status == "demo"
    assert "SYNTHETIC DEMO" in event.result.summary
    assert cursor["analysis"]["request"]["result_id"] == "r7"


def test_profile_worker_persists_dynamic_analysis_and_repository_context(tmp_path):
    worker = Worker(Settings(db_path=str(tmp_path / "run.db"), executor_mode="profile-demo"))
    cid = worker.store.create_conversation()["id"]
    task = PerformanceTask(summary="Inspect", target={"repository": "repo", "profile": "r7"})
    worker.store.update_spec(cid, task, 1)
    rid = worker.store.start_run(cid, 2, uuid4().hex)["id"]
    run = drain_worker(worker, rid)
    assert run["status"] == "demo"
    analysis = run["cursor"]["analysis"]
    assert analysis["analyzer"] == "dynamic-mcp-v2"
    assert analysis["request"]["repository"] == "repo"
    assert analysis["source_context"]["snapshot"]["requested_ref"] == "HEAD"


def test_current_task_overrides_stale_freeform_analysis_details():
    executor = ProfileAnalysisExecutor(analysis_agent())
    task = PerformanceTask(summary="Current goal", target={"profile": "current-id"},
                           details={"profile_analysis": {"result_id": "stale", "goal": "stale"}})
    event, cursor = advance(executor, "run", task, {})
    assert event.type == "result"
    assert cursor["analysis"]["request"]["result_id"] == "current-id"
    assert cursor["analysis"]["request"]["goal"] == "Current goal"


def test_distinct_analyzer_questions_reject_late_previous_answers(tmp_path):
    agent = DynamicProfilerAnalyzer(DemoProfilerGateway(), PlanningModel(questions=["Which workload?", "Which interval?"]))
    worker = Worker(Settings(db_path=str(tmp_path / "questions.db")), executor=ProfileAnalysisExecutor(agent))
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(summary="Inspect", target={"profile": "r7"}), 1)
    rid = worker.store.start_run(cid, 2, uuid4().hex)["id"]
    run = drain_worker(worker, rid)
    first = run["clarification"]["request_id"]
    answer = ClarificationAnswer(request_id=first, answer="Batch")
    worker.store.resume(rid, answer)
    run = drain_worker(worker, rid)
    assert run["clarification"]["request_id"] != first
    with pytest.raises(Conflict, match="different clarification"):
        worker.store.resume(rid, answer)
