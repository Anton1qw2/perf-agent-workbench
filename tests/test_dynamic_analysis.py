import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent import (
    AnalysisRequest,
    AnalysisStep,
    DynamicProfilerAnalyzer,
    ProfileAnalysis,
)
from perf_chat.profiler_analysis_agent.contracts import MCPTool
from perf_chat.profiler_analysis_agent.executor import ProfileAnalysisExecutor
from perf_chat.runtime import execution_deadline
from perf_chat.worker import Worker


def tool(name="inspect_session", schema=None, output=None):
    return {
        "name": name, "description": "Read measurements for the supplied session.",
        "input_schema": schema or {
            "type": "object", "properties": {"session": {"type": "string"}},
            "required": ["session"], "additionalProperties": False,
        }, "output_schema": output,
    }


def gateway(calls, catalog=None, allowed=None, bindings=None, response=None):
    return MCPProfilerGateway(
        "https://profiler.example/mcp",
        caller=lambda name, args: calls.append((name, args)) or (
            response if response is not None else {"cycles": 17, "unit": "cycles"}
        ),
        tool_lister=lambda: catalog if catalog is not None else [tool()],
        allowed_tools=["inspect_session"] if allowed is None else allowed,
        profile_arguments={"inspect_session": "session"} if bindings is None else bindings,
    )


class Model:
    def __init__(self, decisions=None):
        self.decisions = list(decisions or [
            {"action": "call", "reason": "Read measured counters", "tool": "inspect_session",
             "arguments": {"session": "r7"}},
            {"action": "finish", "reason": "Enough measured evidence"},
        ])
        self.plans = []
        self.interpretations = []

    def complete(self, system, payload, **kwargs):
        if payload["schema"]["title"] == "ToolDecision":
            self.plans.append(payload)
            return json.dumps(self.decisions.pop(0))
        self.interpretations.append(payload)
        return json.dumps({
            "observations": [{"text": "The server reports 17 cycles.", "evidence_ids": ["e1"]}],
        })


def request():
    return AnalysisRequest(result_id="r7", goal="Inspect CPU performance")


def test_dynamic_catalog_and_arbitrary_tool_names_with_evidence():
    calls = []
    model = Model()
    report = DynamicProfilerAnalyzer(gateway(calls), model).analyze(request())
    assert report.status == "completed"
    assert calls == [("inspect_session", {"session": "r7"})]
    assert report.evidence[0].operation == "inspect_session"
    assert report.tool_catalog[0]["name"] == "inspect_session"
    assert report.observations[0].evidence_ids == ["e1"]
    assert report.analyzer == "dynamic-mcp-v4"
    assert model.plans[1]["evidence"][0]["response"] == {"cycles": 17, "unit": "cycles"}


@pytest.mark.parametrize("arguments, allowed", [
    ({"session": "other-profile"}, ["inspect_session"]),
    ({"session": 7}, ["inspect_session"]),
    ({"session": "r7", "extra": True}, ["inspect_session"]),
    ({"session": "r7"}, []),
])
def test_gateway_rejects_policy_schema_and_profile_violations(arguments, allowed):
    calls = []
    g = gateway(calls, allowed=allowed)
    with pytest.raises(Exception):
        g.call_tool(MCPTool.model_validate(tool()), arguments, result_id="r7")
    assert not calls


def test_gateway_rejects_catalog_drift_and_output_schema_mismatch():
    calls = []
    catalog = [tool()]
    g = gateway(calls, catalog)
    pinned = g.list_tools()[0]
    catalog[0] = tool(name="different")
    with pytest.raises(ValueError, match="definition changed"):
        g.call_tool(pinned, {"session": "r7"}, result_id="r7")
    assert calls == []
    declared = tool(output={"type": "array"})
    g = gateway(calls, [declared])
    with pytest.raises(Exception):
        g.call_tool(g.list_tools()[0], {"session": "r7"}, result_id="r7")


def test_remote_schema_reference_cannot_fetch_network():
    calls = []
    g = gateway(calls, [tool(schema={"$ref": "https://example.invalid/schema"})])
    with pytest.raises(Exception):
        g.call_tool(g.list_tools()[0], {"session": "r7"}, result_id="r7")
    assert not calls


def test_dynamic_no_model_discovers_without_guessing_calls():
    calls = []
    report = DynamicProfilerAnalyzer(gateway(calls)).analyze(request())
    assert report.status == "insufficient_data" and report.tool_catalog
    assert not calls and not report.observations


def test_catalog_limits_duplicates_and_late_success(monkeypatch):
    g = gateway([], [tool(), tool()])
    with pytest.raises(ValueError, match="unique"):
        g.list_tools()
    g = gateway([], [tool(name=f"t{i}") for i in range(101)])
    with pytest.raises(ValueError, match="100"):
        g.list_tools()
    clock = [0]
    monkeypatch.setattr("perf_chat.runtime.time.monotonic", lambda: clock[0])
    g = gateway([])

    def late_catalog():
        clock[0] = 11
        return [tool()]

    g.tool_lister = late_catalog
    with execution_deadline(10):
        with pytest.raises(TimeoutError):
            g.list_tools()


def test_duplicate_call_and_invalid_evidence_are_not_accepted():
    calls = []
    model = Model()
    model.decisions[1] = dict(model.decisions[0])
    report = DynamicProfilerAnalyzer(gateway(calls), model).analyze(request())
    assert report.status == "insufficient_data" and len(calls) == 1
    assert len(report.evidence) == 1


def test_planning_obeys_one_input_budget():
    calls = []
    report = DynamicProfilerAnalyzer(gateway(calls), Model(), max_input_bytes=200).analyze(request())
    assert report.status == "insufficient_data" and not calls


def test_worker_restart_and_plain_text_clarification_keep_evidence():
    from perf_chat.profiler_analysis_agent.dynamic import DynamicProfilerAnalyzer

    calls = []
    model = Model([
        {"action": "call", "reason": "Read counters", "tool": "inspect_session", "arguments": {"session": "r7"}},
        {"action": "ask", "reason": "Need workload", "question": "Which workload?"},
        {"action": "finish", "reason": "Evidence and workload known"},
    ])
    task = PerformanceTask(summary="Inspect CPU performance", target={"profile": "r7"})
    cursor, answer = {}, None
    saw_question = False
    for _ in range(12):
        # Reconstruct both adapter and analyzer on every durable transition.
        executor = ProfileAnalysisExecutor(DynamicProfilerAnalyzer(gateway(calls), model))
        event, cursor = executor.step("run", task, json.loads(json.dumps(cursor)), answer)
        answer = None
        if event.type == "clarification":
            assert event.clarification.fields == ["analysis_input"]
            saw_question = True
            answer = ClarificationAnswer(request_id=event.clarification.request_id, answer="Batch workload")
        if event.type == "result":
            break
    assert saw_question and event.result.status == "succeeded"
    assert len(calls) == 1
    clarifications = [{"question": "Which workload?", "answer": "Batch workload"}]
    assert model.plans[-1]["clarifications"] == clarifications
    assert model.interpretations[-1]["analysis_context"]["clarifications"] == clarifications
    assert len(cursor["analysis"]["evidence"]) == 1


def test_custom_factory_runs_through_worker_without_ui_changes(tmp_path):
    seen = []

    class Custom:
        analyzer_id = "custom-graph-v1"

        def step_analysis(self, req, *, checkpoint, **kwargs):
            seen.append(checkpoint)
            if not checkpoint:
                return AnalysisStep(checkpoint={"node": "report"}, message="Custom graph progress")
            return AnalysisStep(checkpoint=checkpoint, report=ProfileAnalysis(
                request=req, status="completed", limitations=["Custom graph: no measured findings."],
            ))

    def factory(services):
        assert services.profiler is not None
        assert services.analysis_id
        return Custom()

    settings = Settings(db_path=str(tmp_path / "custom.db"), executor_mode="profile-demo")
    worker = Worker(settings, analyzer_factory=factory)
    cid = worker.store.create_conversation()["id"]
    task = PerformanceTask(summary="Analyze", target={"profile": "r7"})
    worker.store.update_spec(cid, task, 1)
    rid = uuid4().hex
    worker.store.start_run(cid, 2, rid)
    for _ in range(6):
        worker = Worker(settings, analyzer_factory=factory)
        worker.tick()
        if worker.store.run(rid)["status"] == "succeeded":
            break
    assert worker.store.run(rid)["status"] == "succeeded"
    assert seen == [{}, {"node": "report"}]


def test_active_run_cannot_switch_analyzer_implementation():
    one = ProfileAnalysisExecutor(SimpleNamespace(analyzer_id="one"))
    task = PerformanceTask(summary="Analyze", target={"profile": "r7"})
    _, cursor = one.step("run", task, {})
    two = ProfileAnalysisExecutor(SimpleNamespace(analyzer_id="two"))
    event, _ = two.step("run", task, cursor)
    assert event.result.status == "failed"


def test_real_worker_uses_builtin_dynamic_analyzer_after_restart(tmp_path, monkeypatch):
    calls = []
    model = Model([
        {"action": "call", "reason": "Read counters", "tool": "inspect_session",
         "arguments": {"session": "r7"}},
        {"action": "ask", "reason": "Need workload", "question": "Which workload?"},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    monkeypatch.setattr("perf_chat.worker.ChatModel", lambda settings: model)
    monkeypatch.setattr("perf_chat.worker.MCPProfilerGateway", lambda *args, **kwargs: gateway(calls))
    settings = Settings(db_path=str(tmp_path / "dynamic.db"), executor_mode="profile-mcp",
                        mode="llm", model="test", profiler_mcp_url="https://profiler.example/mcp")
    worker = Worker(settings)
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(
        summary="Inspect CPU performance", target={"profile": "r7"},
    ), 1)
    rid = uuid4().hex
    worker.store.start_run(cid, 2, rid)
    question_ids = []
    for _ in range(15):
        worker = Worker(settings)
        worker.tick()
        run = worker.store.run(rid)
        if run["status"] == "waiting":
            question_ids.append(run["clarification"]["request_id"])
            worker.store.resume(rid, ClarificationAnswer(
                request_id=question_ids[-1], answer="Batch workload",
            ))
        if run["status"] in {"succeeded", "failed"}:
            break
    assert run["status"] == "succeeded"
    assert len(question_ids) == 1 and len(calls) == 1
    assert run["cursor"]["analysis"]["analyzer"] == "dynamic-mcp-v4"
    assert len(run["cursor"]["analysis"]["evidence"]) == 1


def test_mcp_sdk_discovery_is_paginated_and_calls_pinned_schema(monkeypatch):
    import sys
    from contextlib import asynccontextmanager
    from types import ModuleType

    mcp = ModuleType("mcp")
    transport_module = ModuleType("mcp.client.streamable_http")
    pages = []
    wire_tool = SimpleNamespace(name="inspect_session", description="Read counters",
                                inputSchema=tool()["input_schema"], outputSchema=None)

    @asynccontextmanager
    async def transport(url, *, http_client):
        assert http_client.follow_redirects is False
        assert http_client.headers["Authorization"] == "Bearer synthetic-secret"
        yield object(), object(), None

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def list_tools(self, *, cursor):
            pages.append(cursor)
            return SimpleNamespace(tools=[] if cursor is None else [wire_tool],
                                   nextCursor="page2" if cursor is None else None)

        async def call_tool(self, name, arguments):
            assert name == "inspect_session" and arguments == {"session": "r7"}
            return SimpleNamespace(isError=False, structuredContent={"cycles": 17}, content=[])

    mcp.ClientSession = Session
    transport_module.streamable_http_client = transport
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", transport_module)
    g = MCPProfilerGateway("https://profiler.example/mcp", token="synthetic-secret",
                           allowed_tools=["inspect_session"],
                           profile_arguments={"inspect_session": "session"})
    pinned = g.list_tools()[0]
    assert g.call_tool(pinned, {"session": "r7"}, result_id="r7") == {"cycles": 17}
    assert pages == [None, "page2", None, "page2"]
    assert "synthetic-secret" not in pinned.model_dump_json()


def test_unknown_evidence_reference_is_rejected_after_dynamic_calls():
    class InvalidModel(Model):
        def complete(self, system, payload, **kwargs):
            if payload["schema"]["title"] == "ToolDecision":
                return super().complete(system, payload, **kwargs)
            return json.dumps({"observations": [{"text": "Unsupported", "evidence_ids": ["unknown"]}]})

    calls = []
    report = DynamicProfilerAnalyzer(gateway(calls), InvalidModel()).analyze(request())
    assert report.status == "insufficient_data" and len(report.evidence) == 1
    assert not report.observations


def test_default_dynamic_factory_is_selected_for_real_and_synthetic_mcp():
    from perf_chat.integrations.profiler_mcp import DemoProfilerGateway
    from perf_chat.profiler_analysis_agent import AnalysisServices, create_builtin_analyzer

    assert isinstance(create_builtin_analyzer(AnalysisServices(profiler=gateway([]))),
                      DynamicProfilerAnalyzer)
    assert isinstance(create_builtin_analyzer(AnalysisServices(profiler=DemoProfilerGateway())),
                          DynamicProfilerAnalyzer)


def test_request_identity_and_expired_steps_reject_late_success():
    analyzer = DynamicProfilerAnalyzer(gateway([]), Model())
    step = analyzer.step_analysis(request(), checkpoint={})
    changed = request().model_copy(update={"result_id": "other"})
    report = analyzer.step_analysis(changed, checkpoint=step.checkpoint).report
    assert report.status == "insufficient_data"
    report = analyzer.step_analysis(request(), checkpoint=step.checkpoint, deadline=0).report
    assert report.status == "insufficient_data" and not report.observations


def test_planner_cannot_invoke_unknown_tool():
    calls = []
    model = Model([{"action": "call", "reason": "Unknown", "tool": "delete_profile",
                    "arguments": {"session": "r7"}}])
    report = DynamicProfilerAnalyzer(gateway(calls), model).analyze(request())
    assert report.status == "insufficient_data" and not calls


def test_dynamic_required_source_keeps_frozen_snapshot_and_citations(tmp_path):
    from perf_chat.profiler_analysis_agent.source_contracts import SourceRequest, SourceSnapshot

    class Source:
        def resolve_revision(self, *args):
            pytest.fail("Must not resolve an accepted snapshot again")

    r = request().model_copy(update={"source": SourceRequest(
        repository="local/repo", ref="main", policy="required",
    )})
    snapshot = SourceSnapshot(repository="local/repo", requested_ref="main", status="unavailable",
                              reason="Source unavailable")
    analyzer = DynamicProfilerAnalyzer(gateway([]), Model(), source_gateway=Source())
    checkpoint = {}
    for _ in range(6):
        step = analyzer.step_analysis(r, checkpoint=checkpoint, source_snapshot=snapshot)
        checkpoint = step.checkpoint
        if step.report:
            break
    assert step.report.status == "needs_input"
    assert step.report.clarification_field == "source"
    assert len(step.report.evidence) == 1


def test_call_budget_still_reports_accepted_evidence():
    calls = []
    model = Model()
    report = DynamicProfilerAnalyzer(gateway(calls), model, max_calls=1).analyze(request())
    assert report.status == "completed" and len(calls) == 1
    assert len(model.plans) == 1
    assert any("budget reached" in note for note in report.limitations)


@pytest.mark.parametrize("response", [[], {}, "", "   "])
@pytest.mark.parametrize("limit", [1, 8])
def test_empty_evidence_cannot_complete_at_budget_or_finish(response, limit):
    calls = []
    model = Model()
    report = DynamicProfilerAnalyzer(gateway(calls, response=response), model,
                                     max_calls=limit).analyze(request())
    assert report.status == "insufficient_data"
    assert not report.observations
    assert not model.interpretations
    assert report.evidence[0].response == response


@pytest.mark.parametrize("change", ["endpoint", "allowlist", "binding"])
def test_pending_call_rejects_connection_or_policy_change(change):
    calls = []
    model = Model()
    g = gateway(calls)
    analyzer = DynamicProfilerAnalyzer(g, model)
    step = analyzer.step_analysis(request(), checkpoint={})
    step = analyzer.step_analysis(request(), checkpoint=step.checkpoint)
    assert step.checkpoint["phase"] == "call"
    if change == "endpoint":
        g.url = "https://other-profiler.example/mcp"
    elif change == "allowlist":
        g.allowed_tools = frozenset(["inspect_session", "another_tool"])
    else:
        g.profile_arguments = {}
    step = DynamicProfilerAnalyzer(g, model).step_analysis(request(), checkpoint=step.checkpoint)
    assert step.report.status == "insufficient_data" and not calls
    assert any("endpoint or tool policy changed" in note for note in step.report.limitations)


def test_connection_fingerprint_contains_no_endpoint_or_credentials():
    g = gateway([])
    g.url = "https://profiler.example/mcp?private=synthetic-private-value"
    g._token = "synthetic-token"
    state = DynamicProfilerAnalyzer(g, Model()).step_analysis(request(), checkpoint={}).checkpoint
    serialized = json.dumps(state)
    assert "synthetic-private-value" not in serialized
    assert "synthetic-token" not in serialized
    assert len(state["connection_fingerprint"]) == 64


def test_relative_clarification_answer_keeps_question_for_next_tool_selection():
    question = "Which workload: batch or interactive?"

    class ContextModel(Model):
        def complete(self, system, payload, **kwargs):
            if payload["schema"]["title"] == "ToolDecision" and payload.get("clarifications"):
                assert payload["clarifications"] == [{"question": question, "answer": "The second one"}]
            return super().complete(system, payload, **kwargs)

    calls = []
    model = ContextModel([
        {"action": "ask", "reason": "Need workload", "question": question},
        {"action": "call", "reason": "Read selected workload", "tool": "inspect_session",
         "arguments": {"session": "r7"}},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    checkpoint, answer = {}, None
    for _ in range(8):
        step = DynamicProfilerAnalyzer(gateway(calls), model).step_analysis(
            request(), checkpoint=json.loads(json.dumps(checkpoint)), answer=answer,
        )
        answer = None
        checkpoint = step.checkpoint
        if step.report:
            if step.report.status == "needs_input":
                answer = "The second one"
            else:
                break
    assert step.report.status == "completed"
    assert len(calls) == 1
    assert model.interpretations[-1]["analysis_context"]["clarifications"] == [
        {"question": question, "answer": "The second one"},
    ]



def test_mcp_defaults_require_allowlist_but_no_profile_argument_map():
    settings = Settings()
    assert settings.profiler_allowed_tools == []
    assert settings.profiler_profile_arguments == {}
    g = MCPProfilerGateway("https://profiler.example/mcp")
    assert g.allowed_tools == frozenset()
    assert g.profile_arguments == {}


def test_dynamic_builds_nested_arguments_from_discovered_schema_without_bindings():
    calls = []
    schema = {
        "type": "object", "required": ["selection", "view"], "additionalProperties": False,
        "properties": {
            "selection": {"type": "object", "required": ["handle"],
                          "properties": {"handle": {"type": "string"}},
                          "additionalProperties": False},
            "view": {"enum": ["cpu", "memory"]},
        },
    }
    arguments = {"selection": {"handle": "r7"}, "view": "cpu"}
    model = Model([
        {"action": "call", "reason": "Inspect requested profile", "tool": "read_measurements",
         "arguments": arguments},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    g = MCPProfilerGateway(
        "https://profiler.example/mcp", allowed_tools=["read_measurements"],
        tool_lister=lambda: [tool("read_measurements", schema)],
        caller=lambda name, args: calls.append((name, args)) or {"cycles": 17},
    )
    report = DynamicProfilerAnalyzer(g, model).analyze(request())
    assert report.status == "completed"
    assert calls == [("read_measurements", arguments)]
    assert g.profile_arguments == {}
    assert model.plans[0]["tools"][0]["input_schema"] == schema


def test_schema_argument_repair_happens_before_any_profiler_call():
    calls = []
    model = Model([
        {"action": "call", "reason": "Read counters", "tool": "inspect_session",
         "arguments": {"session": 7}},
        {"action": "call", "reason": "Use the known opaque identifier", "tool": "inspect_session",
         "arguments": {"session": "r7"}},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    g = gateway(calls, bindings={})
    report = DynamicProfilerAnalyzer(g, model).analyze(request())
    assert report.status == "completed"
    assert calls == [("inspect_session", {"session": "r7"})]
    feedback = model.plans[1]["argument_feedback"]
    assert feedback["rule"] == "type" and feedback["path"] == ["session"]
    assert "7" not in json.dumps(feedback)
    assert model.plans[2]["argument_feedback"] is None


def test_zero_argument_tool_has_no_implicit_profile_or_name_based_gate():
    calls = []
    # This server's read-only tool name alone does not identify its capability.
    definition = tool("get_asm", {"type": "object", "properties": {},
                                  "additionalProperties": False})
    definition["description"] = "Read the global profiler service counters; no assembly output."
    model = Model([
        {"action": "call", "reason": "Inspect profiler service", "tool": "get_asm",
         "arguments": {}},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    g = gateway(calls, [definition], allowed=["get_asm"], bindings={})
    assert request().include_asm is False
    report = DynamicProfilerAnalyzer(g, model).analyze(request())
    assert report.status == "completed"
    assert calls == [("get_asm", {})]


def test_host_generated_source_question_retains_answer_pairing():
    calls = []
    model = Model([
        {"action": "call", "reason": "Read counters", "tool": "inspect_session",
         "arguments": {"session": "r7"}},
        {"action": "source", "reason": "Need source", "locations": [{"path": "hot.c"}]},
        {"action": "ask", "reason": "Need scope", "question": "Which workload?"},
        {"action": "finish", "reason": "Enough evidence"},
    ])
    agent = DynamicProfilerAnalyzer(gateway(calls), model)
    checkpoint = {}
    questions = []
    answers = ["Continue without source", "Batch"]
    answer = None
    for _ in range(20):
        step = agent.step_analysis(request(), checkpoint=checkpoint, answer=answer)
        checkpoint = step.checkpoint
        answer = None
        if step.report and step.report.status == "needs_input":
            questions.append(step.report.open_questions[0])
            answer = answers[len(questions) - 1]
        elif step.report:
            assert step.report.status == "completed"
            break
    else:
        raise AssertionError("Analysis did not complete")
    expected = [{"question": q, "answer": a} for q, a in zip(questions, answers, strict=True)]
    assert model.plans[-1]["clarifications"] == expected
    assert model.interpretations[-1]["analysis_context"]["clarifications"] == expected
