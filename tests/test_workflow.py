import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from perf_chat.api import create_app
from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, ExecutionEvent, PerformanceTask
from perf_chat.llm import ChatModel
from perf_chat.store import Conflict, Store
from perf_chat.task_agent.agent import TaskAgent
from perf_chat.ui.tui import PerformanceChat
from perf_chat.worker import Worker


@pytest.fixture
def system(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.sqlite3"))
    store = Store(settings.db_path)
    worker = Worker(settings)
    client = TestClient(create_app(settings))
    cid = client.post("/api/conversations").json()["id"]
    return client, store, worker, cid


def complete_spec(client, cid):
    state = client.get(f"/api/conversations/{cid}").json()
    spec = state["spec"]
    spec["target"] = {"repository": "local/repo", "branch": "main", "scope": "GET /search"}
    spec["summary"] = "Speed up search"
    spec["objective"] = {
        "metric": "latency_p95",
        "direction": "minimize",
        "target": 200,
        "unit": "ms",
    }
    spec["benchmark"] = {"scenario": "search", "environment": "staging", "concurrency": 50}
    response = client.put(
        f"/api/conversations/{cid}/spec",
        json={
            "spec": spec,
            "expected_version": spec["spec_version"],
        },
    )
    assert response.status_code == 200
    return response.json()["spec"]


def start(client, cid, version):
    rid = uuid4().hex
    response = client.post(
        f"/api/conversations/{cid}/runs",
        json={
            "spec_version": version,
            "request_id": rid,
        },
    )
    assert response.status_code == 202, response.text
    return rid


def test_intake_graph_and_command_idempotency(system):
    client, store, worker, cid = system
    body = {"content": "Speed up search; target.scope=GET /search", "request_id": uuid4().hex}
    for _ in range(2):
        assert client.post(f"/api/conversations/{cid}/messages", json=body).status_code == 202
    assert worker.tick()
    state = store.conversation(cid)
    assert len(state["messages"]) == 2
    assert state["spec"]["target"]["scope"] == "GET /search"
    assert state["spec"]["baseline"]["value"] is None
    assert state["missing_fields"]
    assert not state["busy"]


def test_execution_pause_restart_resume_and_event_replay(system):
    client, store, worker, cid = system
    spec = complete_spec(client, cid)
    rid = start(client, cid, spec["spec_version"])
    assert worker.tick()
    assert worker.tick()
    run = store.run(rid)
    assert run["status"] == "waiting"
    cursor = store.events(cid, 0)[-1]["id"]
    # Restart all application components; pending question and cursor remain durable.
    worker = Worker(Settings(db_path=store.path))
    answer = {"request_id": run["clarification"]["request_id"], "answer": "Isolated staging"}
    assert client.post(f"/api/runs/{rid}/resume", json=answer).status_code == 202
    assert worker.tick()
    assert worker.tick()
    run = store.run(rid)
    assert run["status"] == "demo"
    assert run["result"]["before"] == []
    assert not run["result"]["correctness_verified"]
    replay = store.events(cid, cursor)
    assert all(e["id"] > cursor for e in replay)
    assert replay[-1]["type"] == "execution.result"
    history = store.conversation(cid)["messages"]
    assert any(m["role"] == "executor" for m in history)
    assert any(m["content"] == "Isolated staging" for m in history)


def test_immutable_run_snapshot_and_optimistic_versions(system):
    client, store, worker, cid = system
    spec = complete_spec(client, cid)
    rid = start(client, cid, spec["spec_version"])
    spec["objective"]["target"] = 100
    body = {"spec": spec, "expected_version": spec["spec_version"]}
    assert client.put(f"/api/conversations/{cid}/spec", json=body).status_code == 200
    assert client.put(f"/api/conversations/{cid}/spec", json=body).status_code == 409
    assert store.run(rid)["task"]["objective"]["target"] == 200
    assert (
        client.post(
            f"/api/conversations/{cid}/runs",
            json={
                "spec_version": spec["spec_version"] + 1,
                "request_id": uuid4().hex,
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize("pause", [False, True])
def test_cancel_queued_or_waiting(system, pause):
    client, store, worker, cid = system
    rid = start(client, cid, complete_spec(client, cid)["spec_version"])
    if pause:
        worker.tick()
        worker.tick()
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 202
    worker.tick()
    assert store.run(rid)["status"] == "cancelled"


def test_missing_fields_and_stale_clarification_rejected(system):
    client, store, worker, cid = system
    assert (
        client.post(
            f"/api/conversations/{cid}/runs",
            json={
                "spec_version": 1,
                "request_id": uuid4().hex,
            },
        ).status_code
        == 409
    )
    rid = start(client, cid, complete_spec(client, cid)["spec_version"])
    worker.tick()
    worker.tick()
    with pytest.raises(Conflict):
        store.resume(rid, ClarificationAnswer(request_id="old", answer="yes"))


def test_failed_provider_does_not_corrupt_spec_or_expose_secrets(system):
    client, store, worker, cid = system

    class BrokenModel:
        def complete(self, *args, **kwargs):
            raise ValueError("SECRET_PROVIDER_RESPONSE")

    from perf_chat.orchestrator.agent import OrchestratorAgent
    from perf_chat.orchestrator.graph import build_intake_graph

    worker.graph = build_intake_graph(TaskAgent(BrokenModel()), OrchestratorAgent())
    client.post(
        f"/api/conversations/{cid}/messages",
        json={
            "content": "speed up",
            "request_id": uuid4().hex,
        },
    )
    worker.tick()
    assert store.conversation(cid)["spec"]["spec_version"] == 1
    assert not store.conversation(cid)["busy"]
    assert "SECRET" not in json.dumps(store.events(cid, 0))


def test_provider_request_and_structured_extraction(monkeypatch):
    captured = []

    def handler(request):
        captured.append(request)
        task = PerformanceTask(summary="parsed")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": task.model_dump_json(),
                        }
                    }
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(handler),
            **kw,
        ),
    )
    model = ChatModel(Settings(mode="llm", model="model-test", api_key="test-key"))
    result = TaskAgent(model).extract(PerformanceTask(task_id="real-id", spec_version=4), [])
    assert result.task_id == "real-id" and result.spec_version == 4
    assert result.summary == "parsed"
    body = json.loads(captured[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert captured[0].headers["Authorization"] == "Bearer test-key"


def test_lease_recovery_fences_old_worker(system):
    client, store, worker, cid = system
    store.message(cid, "test", uuid4().hex)
    old = store.claim()
    with store.tx() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (old["id"],))
    replacement = store.claim()
    output = {"task": store.conversation(cid)["spec"], "reply": "ok"}
    store.finish_intake(old, output)
    assert len(store.conversation(cid)["messages"]) == 1
    store.finish_intake(replacement, output)
    assert len(store.conversation(cid)["messages"]) == 2


def test_cancellation_during_executor_call(system):
    client, store, worker, cid = system
    rid = start(client, cid, complete_spec(client, cid)["spec_version"])

    class RacingExecutor:
        def step(self, *args):
            store.cancel(rid)
            return ExecutionEvent(type="progress", message="late progress"), {"stage": 1}

        def cancel(self, run_id):
            assert run_id == rid

    worker.executor = RacingExecutor()
    worker.tick()
    worker.tick()
    assert store.run(rid)["status"] == "cancelled"


def test_tui_headless_renders_conversation():
    async def scenario():
        app = PerformanceChat("http://unused", "conversation-test")
        state = {
            "id": "conversation-test",
            "spec": PerformanceTask().model_dump(),
            "busy": False,
            "messages": [{"role": "user", "content": "Speed up search"}],
            "runs": [],
            "missing_fields": [],
        }

        class FakeApi:
            async def call(self, *args, **kw):
                return state

            async def events(self, *args):
                while True:
                    await asyncio.sleep(60)
                    if False:
                        yield {}

        app.api = FakeApi()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.state["id"] == "conversation-test"
            assert app.query_one("#chat").lines
            assert not app.query_one("#spec").display
            await pilot.press("f2")
            assert app.query_one("#spec").display
            await pilot.press("f2")
            assert not app.query_one("#spec").display

    asyncio.run(scenario())


def test_task_readiness_is_not_tied_to_fixed_metrics():
    task = PerformanceTask(
        summary="Remove unnecessary allocations while preserving behavior",
        target={"repository": "local/repo", "branch": "feature/perf", "profile": "profile-id-7"},
        details={"acceptance": "Compare allocation profiles for the user scenario"},
    )
    assert task.missing_fields() == []
    task.open_questions = ["How can the user scenario be reproduced?"]
    assert task.missing_fields() == ["open_questions"]


def test_context_only_does_not_replace_chat_task(system):
    client, store, worker, cid = system
    state = store.conversation(cid)
    state["spec"]["target"].update(repository="local/repo", branch="main", profile="db:42")
    store.update_spec(cid, PerformanceTask.model_validate(state["spec"]), 1)
    assert store.conversation(cid)["missing_fields"] == ["summary"]
    store.message(cid, "Reduce memory usage when loading a large file", uuid4().hex)
    worker.tick()
    updated = store.conversation(cid)
    assert updated["missing_fields"] == []
    assert updated["spec"]["target"]["profile"] == "db:42"
    assert updated["spec"]["details"]["conversation_notes"]
    rid = start(client, cid, updated["spec"]["spec_version"])
    assert store.run(rid)["task"]["target"]["branch"] == "main"


@pytest.mark.parametrize("target, expected", [
    ({"profile": "result-7"}, []),
    ({"repository": "local/repo"}, []),
    ({"repository": "local/repo", "branch": "main"}, []),
    ({"repository": "local/repo", "profile": "result-7"}, []),
    ({}, ["target.repository_or_profile"]),
    ({"branch": "main"}, ["target.repository_or_profile"]),
    ({"repository": " ", "profile": " "}, ["target.repository_or_profile"]),
])
def test_launch_accepts_either_repository_or_profile(target, expected):
    task = PerformanceTask(summary="Inspect performance", target=target)
    assert task.missing_fields() == expected
    task.summary = ""
    assert "summary" in task.missing_fields()
    task.open_questions = ["Which workload should be analyzed?"]
    assert "open_questions" in task.missing_fields()


@pytest.mark.parametrize("executor, target", [
    ("demo", {"repository": "local/repo"}),
    ("profile-demo", {"profile": "result-7"}),
    ("profile-demo", {"repository": "local/repo"}),
])
def test_run_with_a_single_analysis_input(tmp_path, executor, target):
    settings = Settings(db_path=str(tmp_path / "single-input.sqlite3"), executor_mode=executor)
    client = TestClient(create_app(settings))
    worker = Worker(settings)
    cid = client.post("/api/conversations").json()["id"]
    state = client.get(f"/api/conversations/{cid}").json()
    state["spec"].update(summary="Inspect performance", target=target)
    response = client.put(f"/api/conversations/{cid}/spec", json={
        "spec": state["spec"], "expected_version": state["spec"]["spec_version"],
    })
    assert response.status_code == 200
    state = response.json()
    assert state["missing_fields"] == []
    rid = start(client, cid, state["spec"]["spec_version"])
    for _ in range(8):
        worker.tick()
        run = worker.store.run(rid)
        if run["status"] == "waiting":
            if executor == "profile-demo":
                assert run["clarification"]["fields"] == ["result_id"]
                answer = "result-7"
            else:
                answer = "Use staging"
            worker.store.resume(rid, ClarificationAnswer(
                request_id=run["clarification"]["request_id"], answer=answer,
            ))
        if run["status"] in {"demo", "failed", "succeeded"}:
            break
    assert worker.store.run(rid)["status"] == "demo"
