import asyncio
from uuid import uuid4

import httpx
import pytest

from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.integrations.profiler_mcp import DemoProfilerGateway
from perf_chat.llm import ChatModel
from perf_chat.profiler_analysis_agent import AnalysisRequest, ProfileAnalysisAgent
from perf_chat.store import Store
from perf_chat.worker import Worker


@pytest.mark.parametrize(
    "options,awaiting",
    [
        (None, None),
        ({"note": "allowed extra notes"}, None),
        ("invalid", "options"),
        ({"metrics": "cycles"}, "metrics"),
        ({"metrics": [str(i) for i in range(7)]}, "metrics"),
        ({"include_asm": []}, "include_asm"),
    ],
)
def test_malformed_options_are_clarified_by_worker(tmp_path, options, awaiting):
    settings = Settings(db_path=str(tmp_path / "options.db"), executor_mode="profile-demo")
    store = Store(settings.db_path)
    worker = Worker(settings)
    cid = store.create_conversation()["id"]
    task = PerformanceTask(
        summary="Inspect",
        target={
            "repository": "repo",
            "branch": "main",
            "profile": "id",
        },
        details={"profile_analysis": options},
    )
    store.update_spec(cid, task, 1)
    rid = store.start_run(cid, 2, uuid4().hex)["id"]
    worker.tick()
    worker.tick()
    run = store.run(rid)
    if awaiting is None:
        assert run["status"] == "demo"
    else:
        assert run["status"] == "waiting"
        assert run["cursor"]["awaiting"] == awaiting


def test_overlong_result_id_answer_can_be_corrected(tmp_path):
    settings = Settings(db_path=str(tmp_path / "id.db"), executor_mode="profile-demo")
    store = Store(settings.db_path)
    worker = Worker(settings)
    cid = store.create_conversation()["id"]
    store.update_spec(
        cid,
        PerformanceTask(
            summary="Inspect",
            target={
                "repository": "repo",
                "branch": "main",
            },
        ),
        1,
    )
    rid = store.start_run(cid, 2, uuid4().hex)["id"]
    worker.tick()
    worker.tick()
    first = store.run(rid)["clarification"]["request_id"]
    store.resume(rid, ClarificationAnswer(request_id=first, answer="x" * 4097))
    worker.tick()
    run = store.run(rid)
    assert run["status"] == "waiting"
    assert run["clarification"]["request_id"] != first
    store.resume(
        rid,
        ClarificationAnswer(
            request_id=run["clarification"]["request_id"],
            answer="valid-id",
        ),
    )
    worker.tick()
    assert store.run(rid)["status"] == "demo"


def test_llm_slow_stream_is_bounded_by_total_timeout(monkeypatch):
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b" "

        async def aclose(self):
            closed.append(True)

    async def handler(request):
        return httpx.Response(200, stream=SlowStream())

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(handler),
            **kw,
        ),
    )
    with pytest.raises(TimeoutError):
        ChatModel(Settings(timeout=0.03)).complete("system", {})
    assert closed


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1, 121])
@pytest.mark.parametrize("field", ["timeout", "profiler_mcp_timeout"])
def test_timeouts_must_be_finite_positive_and_bounded(field, timeout):
    with pytest.raises(ValueError, match="must be finite"):
        Settings(**{field: timeout})


def test_catalog_response_is_retained_at_deadline(monkeypatch):
    import time

    now = [time.monotonic()]
    monkeypatch.setattr("perf_chat.runtime.time.monotonic", lambda: now[0])

    class SlowCatalog(DemoProfilerGateway):
        def get_list_metrics(self, result_id):
            now[0] += 2
            return ["cycles"]

    report = ProfileAnalysisAgent(SlowCatalog()).analyze(
        AnalysisRequest(result_id="id", goal="Inspect"),
        deadline=now[0] + 1,
    )
    assert report.status == "insufficient_data"
    assert report.evidence[0].response == ["cycles"]


def test_conversation_snapshot_omits_raw_run_data(tmp_path):
    settings = Settings(db_path=str(tmp_path / "snapshot.db"), executor_mode="profile-demo")
    store = Store(settings.db_path)
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
    worker = Worker(settings)
    worker.tick()
    worker.tick()
    from fastapi.testclient import TestClient

    from perf_chat.api import create_app

    client = TestClient(create_app(settings))
    snapshot = client.get(f"/api/conversations/{cid}").json()
    assert set(snapshot["runs"][0]) == {"id", "status", "clarification", "result", "progress"}
    assert snapshot["runs"][0]["status"] == "demo"
    full = client.get(f"/api/runs/{rid}").json()
    assert full["cursor"]["analysis"]["evidence"][1]["operation"] == "get_hotspots"


def test_conversation_list_does_not_download_histories(tmp_path):
    import json

    store = Store(str(tmp_path / "list.db"))
    cid = store.create_conversation()["id"]
    store.message(cid, "x" * 100_000, uuid4().hex)
    with store.tx() as db:
        conv = store._get(db, "conversations", cid)
        conv["spec"]["summary"] = "Profile analysis"
        store._save(db, "conversations", conv)
    listed = store.list_conversations()
    assert listed == [{"id": cid, "spec": {"summary": "Profile analysis"}}]
    assert len(json.dumps(listed)) < 500
    assert len(store.conversation(cid)["messages"][0]["content"]) == 100_000
