import json
from dataclasses import replace
from uuid import uuid4

import pytest
from analysis_helpers import advance
from fastapi.testclient import TestClient

from perf_chat.api import create_app
from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.integrations.git_mcp import MCPGitGateway
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent import (
    AnalysisContext,
    AnalysisStep,
    ContextUpdateProposal,
    DynamicProfilerAnalyzer,
    ProfileAnalysis,
)
from perf_chat.profiler_analysis_agent.context_updates import effective_context
from perf_chat.profiler_analysis_agent.executor import ProfileAnalysisExecutor
from perf_chat.profiler_analysis_agent.source_contracts import SourceEvidence
from perf_chat.worker import Worker


def decision(action, **kwargs):
    return {"action": action, "reason": "Resolve analysis context", **kwargs}


def proposal(values, *, revision=0, mode="replace"):
    return decision("update_context", context_update={
        "expected_revision": revision, "values": values, "mode": mode,
    })


def query(server="profiler", value="r7"):
    return decision("call", server=server, tool="inspect", arguments={"reference": value})


def gateway(server, calls, discoveries=None, **kwargs):
    def tools():
        if discoveries is not None:
            discoveries.append(server)
        return [{"name": "inspect", "description": "Read reference data.", "input_schema": {
            "type": "object", "properties": {"reference": {"type": "string"}},
            "required": ["reference"], "additionalProperties": False,
        }}]

    cls = MCPGitGateway if server == "git" else MCPProfilerGateway
    return cls(f"https://{server}.example/mcp", allowed_tools=["inspect"],
               tool_lister=tools, caller=lambda name, args: calls.append((server, args)) or
               {"reference": args["reference"], "value": 17}, **kwargs)


class Model:
    def __init__(self, decisions, bad_citation=None):
        self.decisions = list(decisions)
        self.plans, self.interpretations = [], []
        self.bad_citation = bad_citation

    def complete(self, system, payload, **kwargs):
        if payload["schema"]["title"] == "ToolDecision":
            self.plans.append(payload)
            return json.dumps(self.decisions.pop(0))
        self.interpretations.append(payload)
        return json.dumps({"observations": [{
            "text": "The supplied server response is retained as reference evidence.",
            "evidence_ids": [self.bad_citation or item["id"]],
        } for item in payload["evidence"]]})


def factory(model, calls, discoveries=None, source=None):
    return lambda: ProfileAnalysisExecutor(DynamicProfilerAnalyzer(
        gateway("profiler", calls, discoveries), model,
        git_gateway=gateway("git", calls, discoveries), source_gateway=source,
    ))


def reply(event, text):
    return ClarificationAnswer(request_id=event.clarification.request_id, answer=text)


def profile_change(mode="replace", bad_citation=None):
    calls, discoveries = [], []
    model = Model([query(), decision("ask", question="Provide the new profile ID."),
                   proposal({"result_id": "r8"}, mode=mode), query(value="r8"), decision("finish")],
                  bad_citation=bad_citation)
    build = factory(model, calls, discoveries)
    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "r8"))
    assert event.clarification.fields == ["context_confirmation"]
    before = json.loads(json.dumps(cursor))
    answer = reply(event, "yes")
    event, cursor = build().step("run", task, before, answer)
    assert event.type == "progress" and len(calls) == 1
    # Lease redelivery of the same confirmation creates the identical ledger update.
    again, replayed = build().step("run", task, before, answer)
    assert again == event and replayed == cursor
    assert cursor["context_updates"][0]["current"]["revision"] == 1
    assert cursor["analyzer_state"]["calls"] == 1
    event, cursor = advance(build(), "run", task, cursor)
    return event, cursor, model, calls


@pytest.mark.parametrize("mode, delivered", [("replace", {"e2"}), ("compare", {"e1", "e2"})])
def test_profile_replace_and_compare_preserve_provenance_and_scoped_citations(mode, delivered):
    event, cursor, model, calls = profile_change(mode)
    assert event.result.status == "succeeded"
    report = cursor["analysis"]
    assert {item["id"] for item in model.interpretations[-1]["evidence"]} == delivered
    assert set(report["delivered_evidence_ids"]) == delivered
    assert [item["context"]["result_id"] for item in report["evidence"]] == ["r7", "r8"]
    assert [item["context"]["revision"] for item in report["evidence"]] == [0, 1]
    assert report["request"]["result_id"] == "r8" and len(report["context_updates"]) == 1
    assert len(calls) == cursor["analyzer_state"]["calls"] == 2
    assert cursor["analyzer_state"]["rounds"] == 5
    assert model.interpretations[-1]["analysis_context"]["clarifications"] == [
        {"question": "Provide the new profile ID.", "answer": "r8"},
    ]


def test_replaced_profile_cannot_be_cited_even_though_raw_evidence_is_retained():
    event, cursor, _, _ = profile_change(bad_citation="e1")
    assert event.result.status == "failed"
    assert len(cursor["analysis"]["evidence"]) == 2
    assert cursor["analysis"]["observations"] == []


@pytest.mark.parametrize("confirmation", ["no", "maybe"])
def test_declined_or_ambiguous_confirmation_keeps_old_context_and_original_answer(confirmation):
    calls = []
    model = Model([query(), decision("ask", question="New profile?"),
                   proposal({"result_id": "r8"}), decision("finish")])
    build = factory(model, calls)
    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "r8"))
    event, cursor = advance(build(), "run", task, cursor, reply(event, confirmation))
    assert event.result.status == "succeeded" and not cursor.get("context_updates")
    assert cursor["analysis"]["request"]["result_id"] == "r7"
    assert model.plans[-1]["request"]["context_feedback"].startswith("Context update declined")
    assert cursor["analyzer_state"]["answers"] == ["r8"] and len(calls) == 1


def test_git_only_run_accepts_profile_and_refreshes_after_durable_confirmation(tmp_path, monkeypatch):
    calls, discoveries = [], []
    model = Model([query("git", "repo"), decision("ask", question="Provide a profiler Result ID."),
                   proposal({"result_id": "r7"}), query(), decision("finish")])
    monkeypatch.setattr("perf_chat.worker.ChatModel", lambda settings: model)
    monkeypatch.setattr("perf_chat.worker.MCPProfilerGateway", lambda *a, **kw: gateway("profiler", calls, discoveries))
    monkeypatch.setattr("perf_chat.worker.MCPGitGateway", lambda *a, **kw: gateway("git", calls, discoveries))
    settings = Settings(db_path=str(tmp_path / "resume.db"), mode="llm", model="test",
                        executor_mode="profile-mcp", git_mcp_url="https://git.example/mcp",
                        profiler_mcp_url="https://profiler.example/mcp")
    worker = Worker(settings)
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(summary="Inspect", target={"repository": "repo"}), 1)
    rid = worker.store.start_run(cid, 2, uuid4().hex)["id"]
    confirmed = False
    for _ in range(35):
        worker = Worker(settings)
        worker.tick()
        run = worker.store.run(rid)
        if run["status"] == "waiting":
            confirmation = run["clarification"]["fields"] == ["context_confirmation"]
            worker.store.resume(rid, ClarificationAnswer(
                request_id=run["clarification"]["request_id"], answer="yes" if confirmation else "r7",
            ))
            if confirmation:
                before = list(discoveries)
                Worker(settings).tick()
                accepted = worker.store.run(rid)
                assert discoveries == before and accepted["cursor"]["context_updates"]
                assert accepted["task"]["target"]["profile"] is None
                confirmed = True
        if run["status"] in {"succeeded", "failed"}:
            break
    assert confirmed and run["status"] == "succeeded"
    assert discoveries == ["git", "git", "profiler", "git", "profiler"]
    assert [item["server"] for item in model.interpretations[-1]["evidence"]] == ["git", "profiler"]
    assert len(run["cursor"]["context_updates"]) == 1 and len(calls) == 2


def test_custom_opt_in_context_protocol_and_legacy_signatures():
    received = []

    class Custom:
        analyzer_id = "custom-context-v1"
        supports_context_updates = True

        def step_analysis(self, request, *, checkpoint, answer=None, deadline=None,
                          source_snapshot=None, context_updates=None):
            received.append((request, list(context_updates)))
            if not checkpoint:
                return AnalysisStep(checkpoint={"asked": True}, report=ProfileAnalysis(
                    status="needs_input", request=request, open_questions=["New profile?"],
                    clarification_field="analysis_input",
                ))
            if answer:
                return AnalysisStep(checkpoint={"proposed": True}, context_update=ContextUpdateProposal(
                    expected_revision=0, values={"result_id": answer},
                ))
            return AnalysisStep(report=ProfileAnalysis(status="completed", request=request))

    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(ProfileAnalysisExecutor(Custom()), "custom", task, {})
    event, cursor = advance(ProfileAnalysisExecutor(Custom()), "custom", task, cursor, reply(event, "r8"))
    event, cursor = advance(ProfileAnalysisExecutor(Custom()), "custom", task, cursor, reply(event, "yes"))
    assert event.result.status == "succeeded"
    assert received[-1][0].result_id == "r8" and received[-1][1][0].current.revision == 1

    class Legacy:
        def step_analysis(self, request, *, checkpoint, answer=None, deadline=None, source_snapshot=None):
            return AnalysisStep(report=ProfileAnalysis(status="completed", request=request))

    assert advance(ProfileAnalysisExecutor(Legacy()), "legacy", task, {})[0].result.status == "succeeded"


@pytest.mark.parametrize("values", [{"token": "secret"}, {"endpoint": "https://other"},
                                    {"result_id": None}, {"branch": "x" * 257}])
def test_context_patch_rejects_unsupported_fields_and_invalid_values(values):
    with pytest.raises(ValueError):
        ContextUpdateProposal(expected_revision=0, values=values).apply(AnalysisContext(result_id="r7"))


def test_context_update_limit_and_comparison_scope_are_validated():
    with pytest.raises(ValueError):
        ContextUpdateProposal(expected_revision=8, values={"result_id": "r9"})
    with pytest.raises(ValueError):
        ContextUpdateProposal(expected_revision=0, values={"result_id": "r8", "repository": "other"},
                              mode="compare").apply(AnalysisContext(result_id="r7", repository="repo"))


class Source:
    def __init__(self):
        self.resolved, self.read = [], []

    def resolve_revision(self, repository, ref):
        self.resolved.append((repository, ref))
        return "a" * 40, "b" * 40

    def find_source(self, *args):
        return []

    def read_source(self, repository, commit, location, *, candidate=False):
        self.read.append((repository, commit, location.path))
        return SourceEvidence(id="src_" + location.path, repository=repository, commit=commit,
                              blob="c" * 40, path=location.path, start_line=location.start_line,
                              end_line=location.end_line, text="int hot();", sha256="d" * 64,
                              resolution="provided_location")


def test_branch_change_reprepares_same_commit_and_clears_scoped_selection_but_keeps_required_policy():
    source, calls = Source(), []
    model = Model([query(), decision("source", locations=[{"path": "old.c", "start_line": 1, "end_line": 1}]),
                   decision("ask", question="Which branch?"), proposal({"branch": "next"}),
                   query(), decision("finish")])
    build = factory(model, calls, source=source)
    task = PerformanceTask(summary="Inspect", target={"profile": "r7", "repository": "repo", "branch": "main"},
                           details={"profile_analysis": {"source": {"policy": "required",
                           "commit": "a" * 40, "scope_repository": "repo", "scope_ref": "main",
                           "locations": [{"path": "old.c", "start_line": 1, "end_line": 1}]}}})
    event, cursor = advance(build(), "source", task, {})
    event, cursor = advance(build(), "source", task, cursor, reply(event, "next"))
    event, cursor = advance(build(), "source", task, cursor, reply(event, "yes"))
    assert event.clarification.fields == ["source"]
    assert cursor["options"]["source"] == {"policy": "required"}
    assert cursor["source_snapshot"]["requested_ref"] == "next"
    assert cursor["source_snapshot_history"][0]["snapshot"]["requested_ref"] == "main"
    assert source.resolved == [("repo", "a" * 40), ("repo", "next")]
    event, cursor = advance(build(), "source", task, cursor, reply(event, json.dumps({
        "locations": [{"path": "new.c", "start_line": 1, "end_line": 1}],
    })))
    assert event.result.status == "succeeded"
    assert cursor["analysis"]["source_context"]["evidence"][0]["path"] == "new.c"
    assert cursor["analysis"]["source_history"][0]["evidence"][0]["path"] == "old.c"


def test_repository_change_does_not_forward_old_personal_git_token(tmp_path):
    settings = Settings(db_path=str(tmp_path / "tokens.db"), executor_mode="profile-mcp",
                        git_mcp_url="https://git.example/mcp", git_mcp_token="fallback")
    client = TestClient(create_app(settings))
    state = client.post("/api/conversations").json()
    state["spec"]["summary"] = "Inspect"
    state["spec"]["target"]["repository"] = "repo"
    saved = client.put(f"/api/conversations/{state['id']}/spec", json={
        "spec": state["spec"], "expected_version": 1, "credentials": {"git_token": "personal"},
    }).json()
    worker = Worker(settings)
    rid = worker.store.start_run(state["id"], saved["spec"]["spec_version"], uuid4().hex)["id"]
    run = worker.store.run(rid)
    assert worker._profile_executor(run).agent.git_gateway.auth_headers() == {"Authorization": "Bearer personal"}
    old = AnalysisContext(repository="repo")
    update = {"update_id": "confirmed", "reply_to": "question", "previous": old.model_dump(),
              "current": {**old.model_dump(), "repository": "other", "revision": 1}, "mode": "replace"}
    run["cursor"].update(analysis_context_initial=old.model_dump(), context_updates=[update])
    assert effective_context(PerformanceTask.model_validate(run["task"]), run["cursor"])[0].repository == "other"
    assert worker._profile_executor(run).agent.git_gateway.auth_headers() == {"Authorization": "Bearer fallback"}
    assert Worker(replace(settings, git_mcp_token=""))._profile_executor(run).agent.git_gateway.auth_headers() == {}


def test_confirmation_correlation_and_stale_proposal_do_not_change_context():
    model = Model([query(), decision("ask", question="New ID?"), proposal({"result_id": "r8"}), decision("finish")])
    build = factory(model, [])
    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "r8"))
    with pytest.raises(ValueError, match="confirmation"):
        build().step("run", task, cursor, ClarificationAnswer(request_id="wrong", answer="yes"))
    cursor["pending_context_update"]["proposal"]["expected_revision"] = 1
    event, cursor = advance(build(), "run", task, cursor, reply(event, "yes"))
    assert event.result.status == "succeeded" and not cursor.get("context_updates")
    assert cursor["analysis"]["request"]["result_id"] == "r7"


def test_empty_catalog_refresh_is_drift_and_never_reuses_stale_definitions():
    calls = []
    profiler = gateway("profiler", calls)
    model = Model([query(), decision("ask", question="Compare another profile?"),
                   proposal({"result_id": "r8"}, mode="compare"), decision("finish")])
    def build():
        return ProfileAnalysisExecutor(DynamicProfilerAnalyzer(profiler, model))
    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "r8"))
    event, cursor = build().step("run", task, cursor, reply(event, "yes"))
    profiler.tool_lister = lambda: []
    event, cursor = advance(build(), "run", task, cursor)
    assert event.result.status == "failed"
    assert len(calls) == 1 and len(cursor["analysis"]["evidence"]) == 1
    assert len(model.decisions) == 1 and not model.interpretations


def test_context_changes_do_not_reset_profiler_call_budget():
    calls = []
    model = Model([query(), decision("ask", question="New profile?"), proposal({"result_id": "r8"}),
                   query(value="r8"), decision("finish")])
    def build():
        return ProfileAnalysisExecutor(DynamicProfilerAnalyzer(gateway("profiler", calls), model, max_calls=2))
    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "r8"))
    event, cursor = advance(build(), "run", task, cursor, reply(event, "yes"))
    assert event.result.status == "succeeded" and len(calls) == 2
    assert len(model.decisions) == 1
    assert any("budget reached" in item.lower() for item in cursor["analysis"]["limitations"])


def test_context_change_cannot_authorize_another_mcp_endpoint():
    profiler = gateway("profiler", [])
    model = Model([query(), decision("ask", question="New profile?"), proposal({"result_id": "r8"})])
    def build():
        return ProfileAnalysisExecutor(DynamicProfilerAnalyzer(profiler, model))
    task = PerformanceTask(summary="Inspect", target={"profile": "r7"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "r8"))
    event, cursor = build().step("run", task, cursor, reply(event, "yes"))
    profiler.url = "https://other.example/mcp"
    event, cursor = advance(build(), "run", task, cursor)
    assert event.result.status == "failed" and cursor["analysis"]["evidence"]


def test_ssh_repository_reference_is_supported_without_embedded_passwords():
    assert AnalysisContext(repository="ssh://git@example.com/team/project").repository
    with pytest.raises(ValueError):
        AnalysisContext(repository="ssh://git:secret@example.com/team/project")


def test_clearing_profile_without_git_gateway_is_rejected_before_confirmation():
    model = Model([query(), decision("ask", question="Clear the profile?"),
                   proposal({"result_id": ""}), decision("finish")])

    def build():
        return ProfileAnalysisExecutor(DynamicProfilerAnalyzer(gateway("profiler", []), model))

    task = PerformanceTask(summary="Inspect", target={"profile": "r7", "repository": "repo"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "Clear it"))
    assert event.result.status == "succeeded" and not cursor.get("context_updates")
    assert cursor["analysis"]["request"]["result_id"] == "r7"
    assert "Context update rejected" in model.plans[-1]["request"]["context_feedback"]


def test_profile_clear_is_revalidated_at_confirmation_when_git_access_is_disabled():
    model = Model([query(), decision("ask", question="Clear the profile?"),
                   proposal({"result_id": ""}), decision("finish")])
    calls = []
    build = factory(model, calls)
    task = PerformanceTask(summary="Inspect", target={"profile": "r7", "repository": "repo"})
    event, cursor = advance(build(), "run", task, {})
    event, cursor = advance(build(), "run", task, cursor, reply(event, "Clear it"))
    assert event.clarification.fields == ["context_confirmation"]
    no_git = ProfileAnalysisExecutor(DynamicProfilerAnalyzer(gateway("profiler", calls), model))
    event, rejected = no_git.step("run", task, cursor, reply(event, "yes"))
    assert event.type == "progress" and not rejected.get("context_updates")
    assert effective_context(task, rejected)[0].result_id == "r7"
