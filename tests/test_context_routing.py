import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from perf_chat.api import create_app
from perf_chat.config import Settings
from perf_chat.contracts import PerformanceTask
from perf_chat.experts import ExpertResponse
from perf_chat.experts.defaults import default_experts
from perf_chat.orchestrator.routing import route_intake
from perf_chat.worker import Worker


@pytest.mark.parametrize("target, question, expected", [
    ({"profile": "r7"}, "Why is CPU usage high in this perf profile?", "task"),
    ({"repository": "repo"}, "Why is this function slow?", "task"),
    ({"profile": "r7"}, "Analyze this perf profile", "task"),
    ({"profile": "r7"}, "Почему здесь высокая нагрузка?", "task"),
    ({"repository": "repo"}, "What does this function do?", "task"),
    ({"profile": "r7"}, "Find bottlenecks in my perf profile", "task"),
    ({}, "Analyze this perf profile", "task"),
    ({"profile": "r7"}, "Explain the perf documentation for counters", "consultation"),
    ({"profile": "r7"}, "Find the perf documentation", "consultation"),
    ({"repository": "repo"}, "Explain how to analyze a perf profile", "consultation"),
    ({}, "How do I collect a perf profile?", "consultation"),
    ({"profile": "r7"}, "Analyze this perf profile and explain the documentation", "mixed"),
])
def test_demo_context_priority_respects_explicit_consultation(target, question, expected):
    route = route_intake(default_experts(), None, [{"role": "user", "content": question}],
                         task=PerformanceTask(target=target).model_dump())
    assert route.intent == expected
    assert route.expert_id == (None if expected == "task" else "profiler_docs")


@pytest.mark.parametrize("question, expected", [
    ("Why is CPU usage high in this perf profile?", "task"),
    ("Linux CPU", "consultation"),
    ("/ask profiler_docs Explain perf", "consultation"),
    ("/task Explain the perf documentation", "task"),
    ("What can you do?", "capabilities"),
])
def test_demo_target_context_does_not_swallow_commands_or_short_expert_answers(question, expected):
    pending = {"expert_id": "profiler_docs", "inputs": {"question": "Explain perf"},
               "question": "Which platform?"}
    route = route_intake(default_experts(), None, [{"role": "user", "content": question}], pending,
                         task=PerformanceTask(target={"profile": "r7"}).model_dump())
    assert route.intent == expected


class Docs:
    def __init__(self):
        self.requests = []

    def ask(self, request):
        self.requests.append(request)
        return ExpertResponse(status="answered", answer="Documented counter guidance.")


class IntakeModel:
    """Fixture verifies the routing contract, not live model intent accuracy."""

    def __init__(self, intent):
        self.intent = intent
        self.routes = []

    def complete(self, system, payload, **kwargs):
        title = payload.get("schema", {}).get("title")
        if title == "IntakeRoute":
            self.routes.append(payload)
            return json.dumps({"intent": self.intent,
                               "expert_id": "profiler_docs" if self.intent != "task" else None,
                               "inputs": {"question": payload["messages"][-1]["content"]}})
        if title == "PerformanceTask":
            return json.dumps({"summary": payload["messages"][-1]["content"], "open_questions": []})
        return "The analysis task is ready. Use Run to launch it."


@pytest.mark.parametrize("target", [{"profile": "r7"}, {"repository": "repo"}])
def test_contextual_analysis_prepares_task_for_explicit_run_without_consulting_docs(
    tmp_path, monkeypatch, target,
):
    docs = Docs()
    model = IntakeModel("task")
    monkeypatch.setattr("perf_chat.worker.ChatModel", lambda settings: model)
    settings = Settings(db_path=str(tmp_path / "chat.db"), mode="llm", model="test",
                        executor_mode="profile-mcp", git_mcp_url="https://git.example/mcp")
    worker = Worker(settings, experts=default_experts(docs))
    monkeypatch.setattr(worker, "_profile_executor", lambda run: pytest.fail("Intake must not execute analysis"))
    client = TestClient(create_app(settings))
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(target=target, details={"agreed": "Keep behavior"}), 1)
    assert client.post(f"/api/conversations/{cid}/messages", json={
        "content": "Why is CPU usage high here?", "request_id": uuid4().hex,
    }).status_code == 202
    assert worker.tick()
    conversation = worker.store.conversation(cid)
    assert conversation["missing_fields"] == []
    assert conversation["spec"]["target"] == PerformanceTask(target=target).target.model_dump()
    assert conversation["spec"]["details"] == {"agreed": "Keep behavior"}
    assert conversation["runs"] == [] and docs.requests == []
    payload = model.routes[0]
    assert payload["context"]["task"]["target"] == {"profile": target.get("profile"),
                                                       "repository": target.get("repository"), "branch": None}
    catalog = payload["work_catalog"]
    assert any(item["id"] == "performance_analysis" and item["execution"] == "run"
               and item["executor_mode"] == "profile-mcp" for item in catalog)
    assert any(item["id"] == "profiler_docs" and item["execution"] == "intake" for item in catalog)
    assert client.post(f"/api/conversations/{cid}/runs", json={
        "spec_version": conversation["spec"]["spec_version"], "request_id": uuid4().hex,
    }).status_code == 202


def test_explicit_documentation_preserves_selected_task_and_expert_context_policy(tmp_path, monkeypatch):
    docs = Docs()
    monkeypatch.setattr("perf_chat.worker.ChatModel", lambda settings: IntakeModel("consultation"))
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db"), mode="llm", model="test"),
                    experts=default_experts(docs))
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(summary="Agreed goal", target={"profile": "r7"},
                                                details={"agreed": True}, open_questions=["Which interval?"]), 1)
    original = worker.store.conversation(cid)["spec"]
    worker.store.message(cid, "Explain the counter according to perf documentation", uuid4().hex)
    assert worker.tick()
    conversation = worker.store.conversation(cid)
    assert conversation["spec"] == original and conversation["runs"] == []
    assert conversation["messages"][-1]["content"] == "Documented counter guidance."
    assert docs.requests[0].task is None and docs.requests[0].history == ()
    assert worker.store.task_messages(cid, conversation["messages"]) == []


def test_router_context_uses_current_target_not_old_history_and_whitelists_run_state():
    model = IntakeModel("task")
    task = PerformanceTask(summary="Current scope", target={"repository": "current"},
                           details={"private": "PRIVATE_DETAILS"}).model_dump()
    route_intake(default_experts(), model, [
        {"role": "user", "content": "Old profile r-old"},
        {"role": "user", "content": "Why is this function slow?"},
    ], task=task, runs=[{"id": "run1", "status": "succeeded", "cursor": {"private": "PRIVATE_CURSOR"},
                       "result": {"summary": "PRIVATE_REPORT"}}])
    payload = model.routes[0]
    assert payload["context"]["task"]["target"] == {"repository": "current", "profile": None, "branch": None}
    assert payload["context"]["run"] == {"id": "run1", "status": "succeeded"}
    assert "PRIVATE_" not in json.dumps(payload)


def test_selected_context_shares_router_input_budget():
    model = IntakeModel("task")
    route = route_intake(default_experts(), model, [{"role": "user", "content": "Inspect"}],
                         task=PerformanceTask(summary="x" * 6000).model_dump(), max_bytes=4096)
    assert route.intent == "consultation" and "could not be routed" in route.limitation
    assert model.routes == []


def test_analysis_direction_is_not_a_consultation_expert():
    class Model:
        def complete(self, *args, **kwargs):
            return json.dumps({"intent": "consultation", "expert_id": "performance_analysis"})
    route = route_intake(default_experts(), Model(), [{"role": "user", "content": "Analyze"}])
    assert route.intent == "consultation" and route.expert_id is None
    assert "could not be routed" in route.limitation


def test_analysis_without_selected_source_asks_for_input_without_consulting_docs(tmp_path):
    docs = Docs()
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=default_experts(docs))
    cid = worker.store.create_conversation()["id"]
    worker.store.message(cid, "Analyze the perf profile", uuid4().hex)
    assert worker.tick()
    conversation = worker.store.conversation(cid)
    assert conversation["missing_fields"] == ["target.repository_or_profile"]
    assert "Provide a Git repository or a profiler result ID" in conversation["messages"][-1]["content"]
    assert docs.requests == [] and conversation["runs"] == []


def test_new_contextual_analysis_exits_pending_documentation_after_restart(tmp_path):
    class PendingDocs(Docs):
        def ask(self, request):
            self.requests.append(request)
            return ExpertResponse(status="needs_input", answer="Documentation depends on the platform.",
                                  question="Which platform?")

    docs = PendingDocs()
    settings = Settings(db_path=str(tmp_path / "chat.db"))
    worker = Worker(settings, experts=default_experts(docs))
    cid = worker.store.create_conversation()["id"]
    worker.store.update_spec(cid, PerformanceTask(summary="Original goal", target={"profile": "r7"}), 1)
    worker.store.message(cid, "/ask profiler_docs Explain perf", uuid4().hex)
    assert worker.tick()
    assert worker.store.intake_context(cid)["pending_expert"] is not None
    worker = Worker(settings, experts=default_experts(docs))
    question = "Why is CPU usage high in this perf profile?"
    worker.store.message(cid, question, uuid4().hex)
    assert worker.tick()
    conversation = worker.store.conversation(cid)
    assert worker.store.intake_context(cid)["pending_expert"] is None
    assert conversation["spec"]["details"]["conversation_notes"] == [question]
    assert conversation["spec"]["target"]["profile"] == "r7"
    assert conversation["missing_fields"] == [] and len(docs.requests) == 1
