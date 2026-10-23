import hashlib
import json

import pytest

from perf_chat.config import Settings
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent import AnalysisRequest, DynamicProfilerAnalyzer
from perf_chat.profiler_analysis_agent.context_builder import (
    ContextBudgetExceeded,
    PromptContextBuilder,
    RequiredSourceOmitted,
)
from perf_chat.profiler_analysis_agent.contracts import AnalysisServices, HotspotSpec
from perf_chat.profiler_analysis_agent.factory import create_builtin_analyzer
from perf_chat.profiler_analysis_agent.hotspots import project_evidence, resolve_pointer
from perf_chat.profiler_analysis_agent.knowledge import (
    MAX_DOCUMENT_BYTES,
    DirectoryKnowledgeGateway,
    select_knowledge,
)
from perf_chat.profiler_analysis_agent.source_contracts import (
    SourceContext,
    SourceEvidence,
    SourceRequest,
    SourceSnapshot,
)


def knowledge_tree(root):
    root.mkdir()
    for name, content in {
        "SKILLS.md": "# General\nGuidance is not a measurement.\n",
        "cpu/SKILLS.md": "# CPU\nInvestigate CPU costs.\n",
        "cpu/branches/SKILLS.md": "# Branches\nCheck branch behavior.\n",
        "memory/SKILLS.md": "# Memory\nInvestigate allocations.\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "ignored.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    return root


def test_knowledge_snapshot_hierarchy_hashes_and_scope(tmp_path):
    root = knowledge_tree(tmp_path / "knowledge")
    snapshot = DirectoryKnowledgeGateway(str(root)).snapshot()
    assert snapshot.status == "ready"
    selected = select_knowledge(snapshot, ["cpu/branches/SKILLS.md"])
    assert [item.path for item in selected] == [
        "SKILLS.md", "cpu/SKILLS.md", "cpu/branches/SKILLS.md",
    ]
    assert selected[0].sha256 == hashlib.sha256((root / "SKILLS.md").read_bytes()).hexdigest()
    assert all("ignored.py" not in item.path for item in snapshot.documents)
    with pytest.raises(ValueError):
        select_knowledge(snapshot, ["../secret/SKILLS.md"])


@pytest.mark.parametrize("problem", ["missing", "invalid_utf8", "oversized", "too_many"])
def test_invalid_knowledge_is_explicit_and_never_partial(tmp_path, problem):
    root = tmp_path / "knowledge"
    if problem != "missing":
        root.mkdir()
        (root / "SKILLS.md").write_text("Valid root", encoding="utf-8")
        child = root / "invalid"
        child.mkdir()
        if problem == "invalid_utf8":
            (child / "SKILLS.md").write_bytes(b"\xff\xfe")
        elif problem == "oversized":
            (child / "SKILLS.md").write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
        else:
            for index in range(128):
                path = child / str(index)
                path.mkdir()
                (path / "SKILLS.md").write_text("Guidance", encoding="utf-8")
    snapshot = DirectoryKnowledgeGateway(str(root)).snapshot()
    assert snapshot.status == "unavailable"
    assert snapshot.documents == [] and snapshot.limitations
    assert str(root) not in json.dumps(snapshot.model_dump())


def test_linked_knowledge_is_rejected(tmp_path):
    root = knowledge_tree(tmp_path / "knowledge")
    outside = tmp_path / "outside.md"
    outside.write_text("Outside content", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this Windows account")
    assert DirectoryKnowledgeGateway(str(root)).snapshot().status == "unavailable"


def test_env_setting_is_backend_owned_and_optional(tmp_path, monkeypatch):
    monkeypatch.setattr("perf_chat.config.load_dotenv", lambda: None)
    monkeypatch.setenv("PERF_CHAT_DB", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("PERF_CHAT_MODE", "demo")
    monkeypatch.setenv("PERF_CHAT_EXECUTOR", "demo")
    monkeypatch.setenv("PERF_CHAT_KNOWLEDGE_ROOT", " ./knowledge ")
    monkeypatch.delenv("GIT_MCP_TOKEN", raising=False)
    assert Settings.from_env().knowledge_root == "./knowledge"
    monkeypatch.setenv("PERF_CHAT_KNOWLEDGE_ROOT", "")
    assert Settings.from_env().knowledge_root == ""
    assert DirectoryKnowledgeGateway().snapshot().status == "disabled"


class HotspotModel:
    def __init__(self, *, invalid_hotspot=False, bad_citation=False, question=False):
        self.payloads = []
        self.invalid_hotspot = invalid_hotspot
        self.bad_citation = bad_citation
        self.question = question

    def complete(self, system, payload, **kwargs):
        self.payloads.append(json.loads(json.dumps(payload)))
        title = payload["schema"]["title"]
        hotspot = payload.get("hotspot") or (payload.get("analysis_context") or {}).get("hotspot")
        if title == "KnowledgeSelection":
            return json.dumps({"paths": ["cpu/branches/SKILLS.md" if hotspot and hotspot["label"] == "A"
                                        else "memory/SKILLS.md"]})
        if title == "ToolDecision":
            if not payload["evidence"]:
                result = {"action": "call", "reason": "Read overview", "tool": "read_view",
                          "arguments": {"profile": "r7"}}
            elif hotspot:
                label = hotspot["label"]
                if self.question and label == "A" and not payload["clarifications"]:
                    result = {"action": "ask", "reason": "Need workload", "question": "Which workload?"}
                elif len(payload["evidence"]) == 1:
                    result = {"action": "call", "reason": "Read function detail", "tool": "read_view",
                              "arguments": {"profile": "r7", "function": label}}
                else:
                    result = {"action": "finish", "reason": "Enough local evidence"}
            else:
                selections = [{"label": name, "selections": [{
                    "evidence_id": "e1", "pointer": f"/rows/{index}",
                }]} for index, name in enumerate(["A", "B"])]
                if self.invalid_hotspot:
                    selections[0]["selections"][0]["pointer"] = "/rows/99"
                    self.invalid_hotspot = False
                result = {"action": "hotspots", "reason": "Investigate functions separately", "hotspots": selections}
            return json.dumps(result)
        if "hotspot_findings" in payload:
            return json.dumps({"observations": [observation
                for finding in payload["hotspot_findings"]
                for observation in finding["interpretation"]["observations"]]})
        eid = payload["evidence"][-1]["id"]
        if self.bad_citation and hotspot["label"] == "A":
            eid = "memory/SKILLS.md"
        return json.dumps({"observations": [{
            "text": f"Function {hotspot['label']} has server-reported cost.", "evidence_ids": [eid],
        }]})


def profiler(calls):
    tool = {"name": "read_view", "description": "Read overview or a selected function's measurements.",
            "input_schema": {"type": "object", "properties": {
                "profile": {"type": "string"}, "function": {"type": "string"},
            }, "required": ["profile"], "additionalProperties": False}}

    def read(name, args):
        calls.append(args)
        if "function" in args:
            return {"function": args["function"], "cost": 7, "unit": "samples"}
        return {"rows": [{"function": "A", "cost": 17, "unit": "samples"},
                         {"function": "B", "cost": 12, "unit": "samples"}]}

    return MCPProfilerGateway("https://profiler.example/mcp", allowed_tools=["read_view"],
                              tool_lister=lambda: [tool], caller=read)


def drive(model, knowledge=None, *, mutate=None, max_calls=8, request=None, source_snapshot=None,
          profiler_factory=profiler):
    calls, phases, state = [], [], {}
    request = request or AnalysisRequest(result_id="r7", goal="Investigate CPU cost")
    answer = None
    for index in range(90):
        # Both dependencies and graph are recreated as they are after worker restarts.
        analyzer = DynamicProfilerAnalyzer(profiler_factory(calls), model, knowledge_gateway=knowledge,
                                           max_calls=max_calls)
        step = analyzer.step_analysis(request, checkpoint=json.loads(json.dumps(state)), answer=answer,
                                      source_snapshot=source_snapshot)
        answer = None
        state = step.checkpoint
        phases.append(state.get("phase"))
        if index == 0 and mutate:
            mutate()
        if step.report is not None:
            if step.report.status == "needs_input":
                answer = "Batch workload"
                continue
            return step.report, state, calls, phases
    raise AssertionError("Hotspot graph failed to finish")


def test_hotspots_are_isolated_durable_and_synthesized_without_full_profile(tmp_path):
    root = knowledge_tree(tmp_path / "knowledge")
    snapshots = []

    class CountingKnowledge(DirectoryKnowledgeGateway):
        def snapshot(self):
            snapshots.append(True)
            return super().snapshot()

    old_text = (root / "cpu/SKILLS.md").read_bytes().decode("utf-8")
    model = HotspotModel(question=True)
    report, state, calls, phases = drive(model, CountingKnowledge(str(root)), mutate=lambda: (
        (root / "cpu/SKILLS.md").write_text("CHANGED DURING RUN", encoding="utf-8")
    ))
    assert report.status == "completed", report.limitations
    assert len(snapshots) == 1
    assert calls == [{"profile": "r7"}, {"profile": "r7", "function": "A"},
                     {"profile": "r7", "function": "B"}]
    assert [finding.hotspot.label for finding in report.hotspot_findings] == ["A", "B"]
    assert len(report.observations) == 2
    assert {"aggregate", "knowledge", "hotspot_plan", "hotspot_report", "format_report"} <= set(phases)
    assert state["answers"] == ["Batch workload"]
    assert state["analysis_questions"] == ["Which workload?"]
    for payload in model.payloads:
        hotspot = payload.get("hotspot") or (payload.get("analysis_context") or {}).get("hotspot")
        if hotspot:
            data = json.dumps(payload["evidence"])
            other = "B" if hotspot["label"] == "A" else "A"
            assert f'"function": "{other}"' not in data
        if payload["schema"]["title"] == "Interpretation" and hotspot and hotspot["label"] == "A":
            documents = {item["path"]: item["content"] for item in payload["knowledge"]}
            assert documents["cpu/SKILLS.md"] == old_text
            assert "memory/SKILLS.md" not in documents
    synthesis = next(item for item in model.payloads if "hotspot_findings" in item)
    assert synthesis["evidence"] == [] and '"rows":' not in json.dumps(synthesis)
    assert {ref.path for ref in report.knowledge_sources} == {
        "SKILLS.md", "cpu/SKILLS.md", "cpu/branches/SKILLS.md", "memory/SKILLS.md",
    }
    assert "Knowledge references" in report.chat_report.content
    assert "Hotspot investigations" in report.chat_report.content
    # Formatting is its own restart-safe step, with no new model or MCP work.
    before = len(model.payloads)
    step = DynamicProfilerAnalyzer(profiler(calls), model).step_analysis(
        AnalysisRequest(result_id="r7", goal="Investigate CPU cost"), checkpoint=state,
    )
    assert step.report == report and len(model.payloads) == before and len(calls) == 3


def test_bad_selection_returns_feedback_without_extra_mcp_calls():
    model = HotspotModel(invalid_hotspot=True)
    report, _, calls, _ = drive(model)
    assert report.status == "completed" and len(calls) == 3
    assert any(item.get("argument_feedback") for item in model.payloads)


def test_knowledge_cannot_be_cited_as_measurement():
    report, state, _, _ = drive(HotspotModel(bad_citation=True))
    assert report.hotspot_findings[0].status == "insufficient_data"
    assert report.hotspot_findings[0].interpretation.observations == []
    assert state["hotspot_queue"][0]["raw_interpretation"]
    assert len(report.observations) == 1 and report.observations[0].evidence_ids == ["e3"]
    assert any("incomplete" in note for note in report.limitations)


def test_shared_call_budget_still_interprets_each_selected_hotspot():
    report, state, calls, _ = drive(HotspotModel(), max_calls=1)
    assert report.status == "completed" and len(calls) == 1
    assert len(report.hotspot_findings) == 2
    assert all(item.status == "budget_exhausted" for item in report.hotspot_findings)
    assert state["calls"] == 1


def test_hotspot_planning_input_budget_preserves_other_investigations(monkeypatch):
    build = PromptContextBuilder.build

    def bounded_build(self, model, system, payload, source_context, **kwargs):
        hotspot = payload.get("hotspot")
        if payload["schema"]["title"] == "ToolDecision" and hotspot and hotspot["label"] == "A":
            raise ContextBudgetExceeded("Planner request exceeds total input budget")
        return build(self, model, system, payload, source_context, **kwargs)

    monkeypatch.setattr(PromptContextBuilder, "build", bounded_build)
    report, _, calls, _ = drive(HotspotModel())
    assert report.status == "completed", report.limitations
    assert [item.status for item in report.hotspot_findings] == ["budget_exhausted", "completed"]
    assert calls == [{"profile": "r7"}, {"profile": "r7", "function": "B"}]
    assert any("planning exceeds the input budget" in note for note in report.limitations)


@pytest.mark.parametrize("failure,status,field", [
    (RequiredSourceOmitted, "needs_input", "source"),
    (TimeoutError, "insufficient_data", "analysis_input"),
    (ValueError, "insufficient_data", "analysis_input"),
])
def test_hotspot_planning_preserves_required_source_and_failure_boundaries(monkeypatch, failure, status, field):
    build = PromptContextBuilder.build

    def failing_build(self, model, system, payload, source_context, **kwargs):
        if payload["schema"]["title"] == "ToolDecision" and payload.get("hotspot"):
            raise failure("Required source, deadline or policy failure")
        return build(self, model, system, payload, source_context, **kwargs)

    monkeypatch.setattr(PromptContextBuilder, "build", failing_build)
    calls, state = [], {}
    request = AnalysisRequest(result_id="r7", goal="Investigate CPU cost")
    for _ in range(10):
        step = DynamicProfilerAnalyzer(profiler(calls), HotspotModel()).step_analysis(
            request, checkpoint=state,
        )
        state = step.checkpoint
        if step.report is not None:
            assert step.report.status == status
            assert step.report.clarification_field == field
            assert step.report.hotspot_findings == []
            assert calls == [{"profile": "r7"}]
            break
    else:
        pytest.fail("Hotspot planner failure did not reach its original boundary")


@pytest.mark.parametrize("pointer", ["/rows/99", "/rows/-1", "/missing", "rows/0", "/bad~2escape"])
def test_evidence_projection_rejects_invalid_or_invented_locations(pointer):
    spec = HotspotSpec(label="A", selections=[{"evidence_id": "e1", "pointer": pointer}])
    with pytest.raises(ValueError):
        project_evidence(spec, [{"id": "e1", "response": {"rows": [{"value": 0}]}}])


def test_projection_preserves_zero_false_and_rejects_oversized_context():
    spec = HotspotSpec(label="A", selections=[{"evidence_id": "e1", "pointer": "/a~1b/~0key"}])
    projected = project_evidence(spec, [{"id": "e1", "response": {"a/b": {"~key": False}}}])
    assert projected[0]["response"][0]["value"] is False
    assert resolve_pointer([0], "/0") == 0
    with pytest.raises(ValueError, match="budget"):
        project_evidence(HotspotSpec(label="A", selections=[{"evidence_id": "e1"}]),
                         [{"id": "e1", "response": "x" * 25_000}])


def test_optional_knowledge_shares_model_input_budget():
    payload = {"evidence": [{"id": "e1", "response": {"value": 0}}],
               "knowledge": [{"path": "SKILLS.md", "content": "x" * 20_000}]}
    context = PromptContextBuilder(2000).build(object(), "System", payload, None)
    assert context.payload["knowledge"] == [] and context.evidence_ids == {"e1"}
    assert any("Knowledge" in note for note in context.limitations)


def test_factory_uses_explicit_knowledge_dependency(tmp_path):
    gateway = DirectoryKnowledgeGateway(str(knowledge_tree(tmp_path / "knowledge")))
    analyzer = create_builtin_analyzer(AnalysisServices(profiler=profiler([]), knowledge=gateway))
    assert analyzer.knowledge_gateway is gateway


def test_required_source_is_delivered_before_hotspot_interpretation(monkeypatch):
    snapshot = SourceSnapshot(status="resolved", repository="repo", requested_ref="main",
                              commit="a" * 40, tree="b" * 40)
    source = SourceEvidence(id="s1", repository="repo", commit="a" * 40, blob="c" * 40,
                            path="app.py", start_line=1, end_line=1, text="def work(): pass\n",
                            sha256="d" * 64, resolution="provided_location")

    def collect(*args, **kwargs):
        return SourceContext(snapshot=snapshot, evidence=[source], required_delivery="pending")

    monkeypatch.setattr("perf_chat.profiler_analysis_agent.dynamic.collect_source", collect)
    request = AnalysisRequest(result_id="r7", goal="Inspect", repository="repo", branch="main",
                              source=SourceRequest(repository="repo", ref="main", policy="required",
                                                   locations=[{"path": "app.py", "start_line": 1, "end_line": 1}]))
    model = HotspotModel()
    report, _, _, _ = drive(model, request=request, source_snapshot=snapshot)
    assert report.status == "completed", report.limitations
    assert report.source_context.required_delivery == "delivered"
    assert all("s1" in item.delivered_evidence_ids for item in report.hotspot_findings)
    for payload in model.payloads:
        if payload["schema"]["title"] == "Interpretation":
            assert [item["id"] for item in payload["source_evidence"]] == ["s1"]


def test_old_graph_checkpoints_require_a_new_run_before_calls():
    calls = []
    report = DynamicProfilerAnalyzer(profiler(calls), HotspotModel()).step_analysis(
        AnalysisRequest(result_id="r7", goal="Inspect"), checkpoint={"phase": "plan"},
    ).report
    assert report.status == "insufficient_data" and calls == []
    assert any("graph changed" in note for note in report.limitations)


def test_oversized_local_response_is_retained_without_leaking_into_other_hotspots():
    def large_profiler(calls):
        gateway = profiler(calls)
        caller = gateway.caller

        def read(name, args):
            data = caller(name, args)
            if args.get("function") == "A":
                data["oversized_detail"] = "x" * 25_000
            return data

        gateway.caller = read
        return gateway

    model = HotspotModel()
    report, state, calls, _ = drive(model, profiler_factory=large_profiler)
    assert report.status == "completed" and len(calls) == 3
    assert [item.status for item in report.hotspot_findings] == ["insufficient_data", "completed"]
    assert state["evidence"][1]["response"]["oversized_detail"] == "x" * 25_000
    for payload in model.payloads:
        assert "oversized_detail" not in json.dumps(payload)


def test_invalid_knowledge_branch_falls_back_to_root_without_aborting_analysis(tmp_path):
    class InvalidKnowledgeModel(HotspotModel):
        def complete(self, system, payload, **kwargs):
            if payload["schema"]["title"] == "KnowledgeSelection":
                return json.dumps({"paths": ["../outside/SKILLS.md"]})
            return super().complete(system, payload, **kwargs)

    root = knowledge_tree(tmp_path / "knowledge")
    report, _, _, _ = drive(InvalidKnowledgeModel(), DirectoryKnowledgeGateway(str(root)))
    assert report.status == "completed" and len(report.observations) == 2
    assert {ref.path for ref in report.knowledge_sources} == {"SKILLS.md"}
    assert any("Knowledge branch selection" in note for note in report.limitations)
