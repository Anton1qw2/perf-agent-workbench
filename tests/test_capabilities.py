import json
from uuid import uuid4

import pytest

from perf_chat.config import Settings
from perf_chat.experts import ExpertDefinition, ExpertRegistry, ExpertResponse
from perf_chat.experts.contracts import ExpertQuestion
from perf_chat.orchestrator.agent import OrchestratorAgent
from perf_chat.orchestrator.capabilities import capabilities_reply
from perf_chat.orchestrator.graph import build_intake_graph
from perf_chat.orchestrator.routing import route_intake
from perf_chat.task_agent.agent import TaskAgent
from perf_chat.worker import Worker


@pytest.mark.parametrize("question", [
    "Что ты умеешь?", "Чем можешь помочь?", "Какие агенты доступны?",
    "What can you do?", "How can you help?", "Can you analyze a profile?",
])
def test_demo_capabilities_preserve_task_and_do_not_leak_into_task_history(tmp_path, question):
    settings = Settings(db_path=str(tmp_path / "chat.db"))
    worker = Worker(settings, experts=ExpertRegistry())
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    worker.store.message(cid, question, uuid4().hex)
    assert worker.tick()
    conversation = worker.store.conversation(cid)
    assert conversation["spec"] == original
    reply = conversation["messages"][-1]["content"]
    assert "No consultation experts are registered" in reply
    assert "synthetic" in reply
    assert "Analysis direction: Performance analysis" in reply
    assert "Run" not in reply and "Provide" not in reply
    assert worker.store.intake_context(cid)["route"]["intent"] == "capabilities"
    worker = Worker(settings, experts=ExpertRegistry())
    worker.store.message(cid, "/task Optimize loading", uuid4().hex)
    assert worker.tick()
    task = worker.store.conversation(cid)["spec"]
    assert task["summary"] == "Optimize loading"
    assert task["details"]["conversation_notes"] == ["Optimize loading"]


def test_capabilities_interrupt_pending_expert_without_calling_it(tmp_path):
    calls = []

    class Expert:
        def ask(self, request):
            calls.append(request)
            return ExpertResponse(status="needs_input", answer="Reference needed.",
                                  question="Which platform?")

    registry = ExpertRegistry()
    registry.register(ExpertDefinition(id="custom", name="Custom", description="Custom guidance"),
                      lambda _: Expert())
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=registry)
    cid = worker.store.create_conversation()["id"]
    for question in ["/ask custom Explain", "Что ты умеешь?"]:
        worker.store.message(cid, question, uuid4().hex)
        assert worker.tick()
    assert len(calls) == 1
    assert worker.store.intake_context(cid)["pending_expert"] is None
    reply = worker.store.conversation(cid)["messages"][-1]["content"]
    assert "custom: Custom" in reply and "profiler_docs" not in reply


def test_capabilities_commit_checks_lease_and_deduplicates_without_changing_agreed_task(tmp_path):
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=ExpertRegistry())
    cid = worker.store.create_conversation()["id"]
    worker.store.message(cid, "/task Optimize loading", uuid4().hex)
    assert worker.tick()
    original = worker.store.conversation(cid)["spec"]
    worker.store.message(cid, "What can you do?", uuid4().hex)
    job = worker.store.claim()
    conversation = worker.store.conversation(cid)
    output = worker.graph.invoke({"task": original, "messages": conversation["messages"]})
    worker.store.finish_intake({**job, "token": "stale"}, output)
    assert worker.store.conversation(cid) == conversation
    worker.store.finish_intake(job, output)
    worker.store.finish_intake(job, output)
    updated = worker.store.conversation(cid)
    assert updated["spec"] == original
    assert len(updated["messages"]) == len(conversation["messages"]) + 1


def test_model_semantically_routes_capabilities_without_answer_generation():
    class Model:
        def complete(self, system, payload, **kwargs):
            assert payload["schema"]["title"] == "IntakeRoute"
            assert "capabilities" in system
            return json.dumps({"intent": "capabilities", "expert_id": "invented",
                               "inputs": {"question": "Should be discarded"}})

    graph = build_intake_graph(TaskAgent(), OrchestratorAgent(Model()), experts=ExpertRegistry())
    original = {"summary": "Agreed goal", "details": {"agreed": True}}
    result = graph.invoke({"task": original, "messages": [
        {"role": "user", "content": "Explain the ways this assistant could support my work"},
    ]})
    assert result["task"] == original
    assert result["route"]["expert_id"] is None and result["route"]["inputs"] == {}
    assert "help define" in result["reply"]


@pytest.mark.parametrize("question, intent", [
    ("/task What can you do?", "task"),
    ("/ask custom What can you do?", "consultation"),
    ("Analyze profile result-123", "task"),
    ("What can you do? Change my task to analyze loading", "task"),
])
def test_demo_capabilities_do_not_override_commands_or_task_requests(question, intent):
    assert route_intake(ExpertRegistry(), None, [{"role": "user", "content": question}]).intent == intent


@pytest.mark.parametrize("profiler, git", [(False, False), (True, False), (False, True)])
def test_mcp_capabilities_describe_configuration_without_endpoint_or_token(tmp_path, profiler, git):
    worker = Worker(Settings(
        db_path=str(tmp_path / "chat.db"), executor_mode="profile-mcp",
        profiler_mcp_url="https://example.com/private-profiler",
        git_mcp_url="https://example.com/private-git", git_mcp_token="PRIVATE_TOKEN",
        profiler_allowed_tools=["read_profile"] if profiler else [],
        git_allowed_tools=["read_repository"] if git else [],
    ), experts=ExpertRegistry())
    cid = worker.store.create_conversation()["id"]
    worker.store.message(cid, "What can you do?", uuid4().hex)
    assert worker.tick()
    reply = worker.store.conversation(cid)["messages"][-1]["content"]
    assert f"Profiler MCP: {'yes' if profiler else 'no'}" in reply
    assert f"Git MCP: {'yes' if git else 'no'}" in reply
    assert "does not verify service connectivity" in reply
    assert "private-" not in reply and "PRIVATE_TOKEN" not in reply


def test_expired_capabilities_discard_output_and_remain_isolated(tmp_path, monkeypatch):
    worker = Worker(Settings(db_path=str(tmp_path / "chat.db")), experts=ExpertRegistry())
    cid = worker.store.create_conversation()["id"]
    original = worker.store.conversation(cid)["spec"]
    invoke = worker.graph.invoke
    clock = [0.0]

    def late_invoke(state):
        output = invoke(state)
        clock[0] += 121
        output["reply"] = "PRIVATE_LATE_REPLY"
        return output

    monkeypatch.setattr(worker.graph, "invoke", late_invoke)
    with monkeypatch.context() as patch:
        patch.setattr("perf_chat.runtime.time.monotonic", lambda: clock[0])
        worker.store.message(cid, "What can you do?", uuid4().hex)
        assert worker.tick()
    conversation = worker.store.conversation(cid)
    assert conversation["spec"] == original
    assert "capabilities request exceeded" in conversation["messages"][-1]["content"]
    assert "PRIVATE_LATE_REPLY" not in str(conversation)
    assert worker.store.task_messages(cid, conversation["messages"]) == []
    context = worker.store.intake_context(cid)
    assert context["consultation"] is None
    assert context["route"]["intent"] == "capabilities"


def test_capabilities_do_not_generate_expert_input_schemas():
    class CustomQuestion(ExpertQuestion):
        @classmethod
        def model_json_schema(cls, **kwargs):
            pytest.fail("Displaying expert descriptions must not generate input schemas")

    registry = ExpertRegistry()
    registry.register(ExpertDefinition(id="custom", name="Custom", description="Custom guidance",
                                       input_schema=CustomQuestion),
                      lambda _: pytest.fail("Capabilities must not create experts"))
    assert "custom: Custom" in capabilities_reply(registry)


def test_capabilities_catalog_is_bounded_and_escapes_markdown_without_invoking_factories():
    registry = ExpertRegistry()
    for index in range(32):
        registry.register(ExpertDefinition(id=f"expert_{index}", name="*" * 1000,
                                           description="[link](https://example.com)" * 1000),
                          lambda _: pytest.fail("Capabilities must not create experts"))
    reply = capabilities_reply(registry)
    assert len(reply) < 16000
    assert "[link](https://example.com)" not in reply
