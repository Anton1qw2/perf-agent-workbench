from uuid import uuid4

import pytest
from analysis_helpers import drain_worker

from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.worker import Worker


def waiting_run(tmp_path, options):
    settings = Settings(db_path=str(tmp_path / "chat.db"), executor_mode="profile-demo")
    worker = Worker(settings)
    cid = worker.store.create_conversation()["id"]
    task = PerformanceTask(summary="Inspect performance", target={"profile": "r7"},
                           details={"profile_analysis": options})
    worker.store.update_spec(cid, task, 1)
    rid = worker.store.start_run(cid, 2, uuid4().hex)["id"]
    run = drain_worker(worker, rid)
    assert run["status"] == "waiting"
    return settings, worker, cid, rid, run


def resume(worker, run, text):
    worker.store.resume(run["id"], ClarificationAnswer(
        request_id=run["clarification"]["request_id"], answer=text,
    ))


@pytest.mark.parametrize("answer, expected", [
    ("cpu_samples", ["cpu_samples"]),
    ("cpu time, cache misses", ["cpu time", "cache misses"]),
    ("cycles; instructions\ncache_misses", ["cycles", "instructions", "cache_misses"]),
    ('["cycles", "instructions"]', ["cycles", "instructions"]),
    ('"cycles"', ["cycles"]),
    ("[]", []), ("continue", []), ("I don't know", []),
    ("не знаю", []), ("Я не знаю.", []), ("выбери сам", []),
])
def test_metric_chat_replies_complete_without_json_loop_and_survive_restart(tmp_path, answer, expected):
    settings, worker, cid, rid, run = waiting_run(tmp_path, {"metrics": "invalid-shape"})
    assert run["cursor"]["awaiting"] == "metrics"
    assert "No preselection is required" in run["clarification"]["question"]
    assert worker.tick() is False  # Waiting alone must not enqueue another question.
    resume(worker, run, answer)
    worker = Worker(settings)
    completed = drain_worker(worker, rid)
    assert completed["status"] == "demo"
    assert completed["cursor"]["analysis"]["request"]["metrics"] == expected
    assert completed["task"]["details"]["profile_analysis"] == {"metrics": "invalid-shape"}
    conversation = worker.store.conversation(cid)
    questions = [message for message in conversation["messages"] if message["role"] == "executor"]
    assert len(questions) == 1
    assert "Reply with valid JSON for metrics" not in str(conversation["messages"])


@pytest.mark.parametrize("invalid", ['[bad]', '1', '{"metrics":[]}', 'a,b,c,d,e,f,g'])
def test_invalid_metric_replies_explain_selection_and_allow_continuing_without_it(tmp_path, invalid):
    _, worker, _, rid, run = waiting_run(tmp_path, {"metrics": "invalid-shape"})
    resume(worker, run, invalid)
    updated = drain_worker(worker, rid)
    assert updated["status"] == "waiting"
    assert updated["clarification"]["request_id"] != run["clarification"]["request_id"]
    assert "counter names reported by your profiler" in updated["clarification"]["question"]
    assert "reply 'continue'" in updated["clarification"]["question"]
    resume(worker, updated, "continue")
    assert drain_worker(worker, rid)["status"] == "demo"


@pytest.mark.parametrize("answer", ["{}", "continue", "не знаю"])
def test_accepting_default_options_does_not_reask_after_later_worker_steps(tmp_path, answer):
    settings, worker, cid, rid, run = waiting_run(tmp_path, "invalid-options")
    assert run["cursor"]["awaiting"] == "options"
    resume(worker, run, answer)
    completed = drain_worker(Worker(settings), rid)
    assert completed["status"] == "demo"
    assert completed["cursor"]["analysis_options_confirmed"] is True
    assert completed["cursor"]["analysis"]["request"]["metrics"] == []
    assert len([message for message in worker.store.conversation(cid)["messages"]
                if message["role"] == "executor"]) == 1


def test_metric_correction_remains_accepted_across_another_option_question(tmp_path):
    settings, worker, _, rid, run = waiting_run(tmp_path, {"metrics": "invalid", "include_asm": []})
    resume(worker, run, "continue")
    run = drain_worker(worker, rid)
    assert run["cursor"]["awaiting"] == "include_asm"
    resume(worker, run, "false")
    completed = drain_worker(Worker(settings), rid)
    assert completed["status"] == "demo"
    assert completed["cursor"]["analysis"]["request"]["metrics"] == []
    assert completed["cursor"]["analysis"]["request"]["include_asm"] is False
