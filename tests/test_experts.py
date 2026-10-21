import json
import time
from uuid import uuid4

import pytest

from perf_chat.config import Settings
from perf_chat.contracts import PerformanceTask
from perf_chat.experts import (
    ContextPolicy,
    ExpertDefinition,
    ExpertRegistry,
    ExpertResponse,
    ExpertSource,
)
from perf_chat.experts.defaults import default_experts
from perf_chat.orchestrator.agent import OrchestratorAgent
from perf_chat.orchestrator.graph import build_intake_graph
from perf_chat.orchestrator.routing import route_intake
from perf_chat.task_agent.agent import TaskAgent
from perf_chat.worker import Worker


class HardwareExpert:
    def __init__(self):
        self.requests = []

    def ask(self, request):
        self.requests.append(request)
        if "Linux" not in request.inputs.question:
            return ExpertResponse(status="needs_input", answer="Platform affects the guidance.",
                                  question="Which platform?")
        return ExpertResponse(status="answered", answer="Hardware guidance for Linux.",
                              sources=[ExpertSource(title="CPU reference", url="https://example.com/cpu")],
                              limitations=["Machine configuration is unverified."])


def registry_for(expert, policy=None):
    registry = ExpertRegistry()
    registry.register(ExpertDefinition(
        id="hardware", name="Hardware", description="Explains CPU and memory behavior.",
        context_policy=policy or ContextPolicy(),
    ), factory=lambda services: expert)
    return registry


def send(worker, cid, content):
    worker.store.message(cid, content, uuid4().hex)
    assert worker.tick()
    return worker.store.conversation(cid)


def test_consultation_survives_restart_without_changing_task_or_version(tmp_path):
    expert = HardwareExpert()
    registry = registry_for(expert)
    settings = Settings(db_path=str(tmp_path / "chat.db"))
    worker = Worker(settings, experts=registry)
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    conversation = send(worker, cid, "/ask hardware Explain CPU caches")
    assert conversation["spec"] == original
    assert "Which platform?" in conversation["messages"][-1]["content"]
    assert "repository" not in conversation["messages"][-1]["content"]
    worker = Worker(settings, experts=registry)
    conversation = send(worker, cid, "Linux")
    assert conversation["spec"] == original
    assert "Hardware guidance for Linux" in conversation["messages"][-1]["content"]
    assert "https://example.com/cpu" in conversation["messages"][-1]["content"]
    assert worker.store.intake_context(cid)["pending_expert"] is None
    assert len(expert.requests) == 2
    assert expert.requests[0].task is None and expert.requests[0].history == ()
    # A later task cannot import consultation questions or expert answers as agreements.
    conversation = send(worker, cid, "Optimize file loading")
    assert conversation["spec"]["details"]["conversation_notes"] == ["Optimize file loading"]


def test_task_command_exits_pending_consultation(tmp_path):
    expert = HardwareExpert()
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=registry_for(expert))
    cid = worker.store.create_conversation()["id"]
    send(worker, cid, "/ask hardware Explain cache misses")
    conversation = send(worker, cid, "/task Optimize file loading")
    assert conversation["spec"]["summary"] == "Optimize file loading"
    assert len(expert.requests) == 1
    assert worker.store.intake_context(cid)["pending_expert"] is None


def test_unknown_expert_and_removed_pending_expert_do_not_modify_task(tmp_path):
    settings = Settings(db_path=str(tmp_path / "chat.db"))
    worker = Worker(settings, experts=registry_for(HardwareExpert()))
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    send(worker, cid, "/ask hardware Explain caches")
    worker = Worker(settings, experts=ExpertRegistry())
    conversation = send(worker, cid, "Linux")
    assert conversation["spec"] == original
    assert "unavailable" in conversation["messages"][-1]["content"]
    conversation = send(worker, cid, "/ask unknown Explain caches")
    assert conversation["spec"] == original


@pytest.mark.parametrize("policy", [ContextPolicy(), ContextPolicy(task=True, conversation_history=True)])
def test_expert_context_is_explicit_and_cannot_mutate_host_data(policy):
    class MutatingExpert:
        def ask(self, request):
            if policy.task:
                request.task["summary"] = "Changed"
                request.history[0]["content"] = "Changed"
            else:
                assert request.task is None and request.history == ()
            return ExpertResponse(status="answered", answer="Reference answer")

    task = {"summary": "Original"}
    messages = [{"role": "user", "content": "Original"}]
    response = registry_for(MutatingExpert(), policy).consult(
        "hardware", {"question": "Explain caches"}, task=task, messages=messages,
    )
    assert response.status == "answered"
    assert task["summary"] == messages[0]["content"] == "Original"


@pytest.mark.parametrize("failure", ["provider", "invalid", "missing_question", "oversized", "blank"])
def test_expert_failures_return_bounded_non_echoing_limitations(failure):
    class BrokenExpert:
        def ask(self, request):
            if failure == "provider":
                raise RuntimeError("PRIVATE_PROVIDER_TOKEN")
            if failure == "invalid":
                return {"status": "invented", "answer": "PRIVATE_PROVIDER_TOKEN"}
            if failure == "oversized":
                return ExpertResponse(status="answered", answer="x" * 5000)
            if failure == "blank":
                return ExpertResponse.model_construct(status="answered", answer="   ")
            return ExpertResponse(status="needs_input", answer="PRIVATE_PROVIDER_TOKEN")

    response = registry_for(BrokenExpert()).consult("hardware", {"question": "Explain"}, max_bytes=4096)
    assert response.status == "unavailable"
    assert "PRIVATE_PROVIDER_TOKEN" not in response.model_dump_json()


def test_expired_deadline_and_oversized_inputs_never_invoke_expert():
    expert = HardwareExpert()
    registry = registry_for(expert)
    assert registry.consult("hardware", {"question": "Explain"},
                            deadline=time.monotonic() - 1).status == "unavailable"
    assert registry.consult("hardware", {"question": "x" * 5000},
                            max_bytes=4096).status == "unavailable"
    assert expert.requests == []


class RoutingModel:
    def __init__(self, decision):
        self.decision = decision
        self.payloads = []

    def complete(self, system, payload, **kwargs):
        self.payloads.append(payload)
        if payload.get("schema", {}).get("title") == "IntakeRoute":
            return json.dumps(self.decision)
        return "Task response"


@pytest.mark.parametrize("intent", ["consultation", "mixed", "task", "capabilities"])
def test_model_routes_before_task_extraction_and_calls_at_most_one_expert(intent):
    expert = HardwareExpert()
    model = RoutingModel({"intent": intent, "expert_id": "hardware",
                          "inputs": {"question": "CPU on Linux"}})

    class RecordingTaskAgent:
        def __init__(self):
            self.calls = 0

        def extract(self, task, messages):
            self.calls += 1
            return task.model_copy(update={"summary": "Updated goal"})

    task_agent = RecordingTaskAgent()
    graph = build_intake_graph(task_agent, OrchestratorAgent(model), experts=registry_for(expert))
    original = PerformanceTask(summary="Original goal").model_dump()
    result = graph.invoke({"task": original, "messages": [{"role": "user", "content": "Request"}]})
    assert task_agent.calls == (0 if intent in {"consultation", "capabilities"} else 1)
    assert len(expert.requests) == (1 if intent in {"consultation", "mixed"} else 0)
    if intent in {"consultation", "capabilities"}:
        assert result["task"] == original
        assert "Task response" not in result["reply"]
    if intent == "capabilities":
        assert len(model.payloads) == 1
        assert "hardware" in result["reply"]
        assert result["route"]["expert_id"] is None
    if intent == "mixed":
        assert "Hardware guidance" in result["reply"] and "Task response" in result["reply"]


def test_invalid_router_output_does_not_fall_back_to_task_changes():
    model = RoutingModel({"intent": "mixed", "expert_id": "unknown"})
    graph = build_intake_graph(TaskAgent(), OrchestratorAgent(model), experts=ExpertRegistry())
    original = PerformanceTask(summary="Agreed task").model_dump()
    result = graph.invoke({"task": original, "messages": [{"role": "user", "content": "Explain"}]})
    assert result["task"] == original
    assert result["route"]["intent"] == "consultation"
    assert "could not be routed" in result["reply"]


def test_routing_budget_is_checked_before_model_call():
    model = RoutingModel({"intent": "task"})
    decision = route_intake(ExpertRegistry(), model,
                            [{"role": "user", "content": "x" * 5000}], max_bytes=4096)
    assert decision.intent == "consultation"
    assert model.payloads == []


def test_stale_and_duplicate_intake_commits_cannot_duplicate_consultation(tmp_path):
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=registry_for(HardwareExpert()))
    cid = worker.store.create_conversation()["id"]
    worker.store.message(cid, "/ask hardware CPU on Linux", uuid4().hex)
    job = worker.store.claim()
    original = worker.store.conversation(cid)["spec"]
    output = worker.graph.invoke({"task": original, "messages": [
        {"role": "user", "content": "/ask hardware CPU on Linux"},
    ]})
    worker.store.finish_intake({**job, "token": "stale"}, output)
    assert worker.store.intake_context(cid) == {}
    worker.store.finish_intake(job, output)
    worker.store.finish_intake(job, output)
    conversation = worker.store.conversation(cid)
    assert len(conversation["messages"]) == 2 and conversation["spec"] == original


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://token@example.com", "https://example.com/<bad>"])
def test_expert_sources_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        ExpertSource(title="Reference", url=url)


def test_constructed_nested_source_is_rejected_before_graph_rendering():
    class InvalidSourceExpert:
        def ask(self, request):
            source = ExpertSource.model_construct(title="Reference", url="javascript:alert(1)")
            return ExpertResponse.model_construct(status="answered", answer="Guidance", sources=[source])

    graph = build_intake_graph(TaskAgent(), OrchestratorAgent(),
                               experts=registry_for(InvalidSourceExpert()))
    task = PerformanceTask(summary="Agreed task").model_dump()
    result = graph.invoke({"task": task, "messages": [
        {"role": "user", "content": "/ask hardware Explain caches"},
    ]})
    assert result["consultation"]["status"] == "unavailable"
    assert "javascript" not in result["reply"]
    assert result["task"] == task


def test_duplicate_registration_is_rejected():
    registry = registry_for(HardwareExpert())
    with pytest.raises(ValueError, match="unique"):
        registry.register(ExpertDefinition(id="hardware", name="Other", description="Other"), lambda _: None)


def test_mixed_reply_does_not_reintroduce_expert_guidance_into_task_history(tmp_path):
    model = RoutingModel({"intent": "mixed", "expert_id": "hardware",
                          "inputs": {"question": "CPU on Linux"}})
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")))
    worker.graph = build_intake_graph(TaskAgent(), OrchestratorAgent(model),
                                      experts=registry_for(HardwareExpert()))
    cid = worker.store.create_conversation()["id"]
    conversation = send(worker, cid, "Explain CPU caches and optimize loading")
    assert "Hardware guidance" in conversation["messages"][-1]["content"]
    task_history = worker.store.task_messages(cid, conversation["messages"])
    assert task_history[-1]["content"] == "Task response"
    assert task_history[0]["content"] == "Explain CPU caches and optimize loading"


def test_late_expert_success_is_rejected(monkeypatch):
    clock = [0.0]

    class LateExpert:
        def ask(self, request):
            clock[0] = 2.0
            return ExpertResponse(status="answered", answer="Late guidance")

    with monkeypatch.context() as patch:
        patch.setattr("perf_chat.runtime.time.monotonic", lambda: clock[0])
        response = registry_for(LateExpert()).consult("hardware", {"question": "Explain"}, deadline=1.0)
    assert response.status == "unavailable"
    assert "Late guidance" not in response.answer


def test_input_schema_is_validated_before_factory_is_called():
    from pydantic import BaseModel, ConfigDict

    class HardwareQuestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str
        cpu_model: str

    registry = ExpertRegistry()
    calls = []
    registry.register(ExpertDefinition(id="hardware", name="Hardware", description="CPU reference",
                                       input_schema=HardwareQuestion),
                      lambda services: calls.append(services))
    response = registry.consult("hardware", {"question": "Explain"})
    assert response.status == "unavailable" and calls == []


def test_profiler_uses_common_llm_catalog_even_when_demo_triggers_are_disabled(tmp_path, monkeypatch):
    from perf_chat.profiler_docs_agent import trigger

    path = tmp_path / "triggers.txt"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(trigger, "TRIGGER_PATH", path)
    registry = default_experts()
    model = RoutingModel({"intent": "consultation", "expert_id": "profiler_docs",
                          "inputs": {"question": "Explain Perf"}})
    decision = route_intake(registry, model, [{"role": "user", "content": "Explain Perf"}])
    assert any(item["id"] == "profiler_docs" and item["intent"] == "consultation"
               for item in model.payloads[0]["work_catalog"])
    assert decision.expert_id == "profiler_docs"
    decision = route_intake(registry, model, [{"role": "user", "content": "/ask profiler_docs Explain Perf"}])
    assert decision.expert_id == "profiler_docs"
    assert len(model.payloads) == 1


def test_llm_followup_receives_durable_pending_question():
    model = RoutingModel({"intent": "consultation", "expert_id": "hardware",
                          "inputs": {"question": "CPU caches on Linux"}})
    pending = {"expert_id": "hardware", "inputs": {"question": "Explain CPU caches"},
               "question": "Which platform?"}
    decision = route_intake(registry_for(HardwareExpert()), model,
                            [{"role": "user", "content": "Linux"}], pending)
    assert decision.expert_id == "hardware"
    assert model.payloads[0]["pending"] == pending


@pytest.mark.parametrize("failure", ["expert", "router"])
def test_pending_question_survives_transient_failure_and_retry_after_restart(tmp_path, failure):
    class TransientExpert(HardwareExpert):
        fail_once = True

        def ask(self, request):
            if failure == "expert" and "Linux" in request.inputs.question and self.fail_once:
                self.fail_once = False
                raise RuntimeError("Temporary private provider failure")
            return super().ask(request)

    class FailedRouter:
        def complete(self, *args, **kwargs):
            raise RuntimeError("Temporary private routing failure")

    expert = TransientExpert()
    registry = registry_for(expert)
    settings = Settings(db_path=str(tmp_path / "chat.db"))
    worker = Worker(settings, experts=registry)
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    send(worker, cid, "/ask hardware Explain caches")
    pending = worker.store.intake_context(cid)["pending_expert"]
    if failure == "router":
        worker.graph = build_intake_graph(TaskAgent(), OrchestratorAgent(FailedRouter()), experts=registry)
    conversation = send(worker, cid, "Linux")
    assert conversation["spec"] == original
    assert worker.store.intake_context(cid)["pending_expert"] == pending
    worker = Worker(settings, experts=registry)
    conversation = send(worker, cid, "Linux")
    assert "Hardware guidance for Linux" in conversation["messages"][-1]["content"]
    assert conversation["spec"] == original
    assert worker.store.intake_context(cid)["pending_expert"] is None


@pytest.mark.parametrize("intent", ["task", "mixed"])
def test_worker_rejects_late_graph_result_before_saving_task_or_reply(tmp_path, monkeypatch, intent):
    clock = [0.0]

    class LateGraph:
        def invoke(self, state):
            clock[0] = 121.0
            return {"task": {**state["task"], "summary": "Late accepted task"},
                    "reply": "Late answer", "route": {"intent": intent}}

    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")))
    worker.graph = LateGraph()
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    with monkeypatch.context() as patch:
        patch.setattr("perf_chat.runtime.time.monotonic", lambda: clock[0])
        conversation = send(worker, cid, "Optimize loading")
    assert conversation["spec"] == original
    assert not any(message["content"] == "Late answer" for message in conversation["messages"])
    assert worker.store.intake_context(cid) == {}


def test_limitation_item_bounds_are_declared_in_schema_and_enforced():
    schema = ExpertResponse.model_json_schema()
    assert schema["properties"]["limitations"]["items"]["maxLength"] == 2000
    assert ExpertResponse(status="answered", answer="Answer", limitations=["x" * 2000])
    with pytest.raises(ValueError):
        ExpertResponse(status="answered", answer="Answer", limitations=["x" * 2001])


@pytest.mark.parametrize("alias_mode", ["alias", "separate_aliases", "alias_path"])
def test_validated_custom_inputs_keep_aliases_and_computed_fields(alias_mode):
    from pydantic import AliasPath, BaseModel, ConfigDict, Field, computed_field

    field_options = {
        "alias": {"alias": "cpuModel"},
        "separate_aliases": {"validation_alias": "cpuModel", "serialization_alias": "cpuDescription"},
        "alias_path": {"validation_alias": AliasPath("hardware", "cpu"), "serialization_alias": "cpuDescription"},
    }[alias_mode]

    class HardwareQuestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str
        cpu_model: str = Field(**field_options)

        @computed_field
        @property
        def cpu_label(self) -> str:
            return "CPU: " + self.cpu_model

    class RecordingExpert:
        def ask(self, request):
            assert isinstance(request.inputs, HardwareQuestion)
            assert request.inputs.cpu_model == "Example CPU"
            assert request.inputs.cpu_label == "CPU: Example CPU"
            return ExpertResponse(status="answered", answer="Reference guidance")

    registry = ExpertRegistry()
    registry.register(ExpertDefinition(id="hardware", name="Hardware", description="CPU reference",
                                       input_schema=HardwareQuestion), lambda _: RecordingExpert())
    fields = {"hardware": {"cpu": "Example CPU"}} if alias_mode == "alias_path" else {"cpuModel": "Example CPU"}
    response = registry.consult("hardware", {"question": "Explain", **fields})
    assert response.status == "answered"


@pytest.mark.parametrize("pending_question", [False, True])
def test_expired_consultation_remains_isolated_from_future_task_history(tmp_path, monkeypatch, pending_question):
    clock = [0.0]

    class LateExpert(HardwareExpert):
        def ask(self, request):
            if "Linux" not in request.inputs.question:
                return super().ask(request)
            clock[0] += 121.0
            return ExpertResponse(status="needs_input", answer="PRIVATE_LATE_GUIDANCE",
                                  question="PRIVATE_LATE_QUESTION")

    settings = Settings(db_path=str(tmp_path / "chat.db"))
    registry = registry_for(LateExpert())
    worker = Worker(settings, experts=registry)
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    if pending_question:
        send(worker, cid, "/ask hardware Explain caches")
    pending = worker.store.intake_context(cid).get("pending_expert")
    message = "Linux" if pending_question else "/ask hardware Explain caches on Linux"
    with monkeypatch.context() as patch:
        patch.setattr("perf_chat.runtime.time.monotonic", lambda: clock[0])
        conversation = send(worker, cid, message)
    assert conversation["spec"] == original
    assert "exceeded its deadline" in conversation["messages"][-1]["content"]
    context = worker.store.intake_context(cid)
    assert context["pending_expert"] == pending
    assert context["consultation"]["status"] == "unavailable"
    assert "PRIVATE_LATE" not in json.dumps(context)
    assert "PRIVATE_LATE" not in str(conversation["messages"])
    worker = Worker(settings, experts=registry)
    conversation = send(worker, cid, "/task Optimize loading")
    assert conversation["spec"]["summary"] == "Optimize loading"
    assert conversation["spec"]["details"]["conversation_notes"] == ["Optimize loading"]


@pytest.mark.parametrize("selected_id", ["hardware", "other"])
def test_expired_explicit_selection_does_not_revive_previous_pending(tmp_path, monkeypatch, selected_id):
    clock = [0.0]

    class LateExpert(HardwareExpert):
        def ask(self, request):
            if "Linux" in request.inputs.question:
                clock[0] += 121.0
                return ExpertResponse(status="needs_input", answer="Late guidance", question="Late question")
            return super().ask(request)

    registry = registry_for(LateExpert())
    registry.register(ExpertDefinition(id="other", name="Other", description="Other reference"),
                      lambda _: LateExpert())
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=registry)
    cid = worker.store.create_conversation()["id"]
    send(worker, cid, "/ask hardware Explain caches")
    assert worker.store.intake_context(cid)["pending_expert"] is not None
    with monkeypatch.context() as patch:
        patch.setattr("perf_chat.runtime.time.monotonic", lambda: clock[0])
        conversation = send(worker, cid, f"/ask {selected_id} Explain Linux")
    assert "exceeded its deadline" in conversation["messages"][-1]["content"]
    assert worker.store.intake_context(cid)["pending_expert"] is None
