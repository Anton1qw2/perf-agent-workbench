import asyncio
from contextlib import suppress
from uuid import uuid4

from perf_chat.config import Settings
from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.store import Store
from perf_chat.task_agent.agent import TaskAgent
from perf_chat.ui.tui import PerformanceChat
from perf_chat.worker import Worker


def test_partial_model_reply_preserves_existing_task():
    class PartialModel:
        def complete(self, *args, **kwargs):
            return '{"summary":"Updated", "target":{"branch":"perf"}}'

    current = PerformanceTask(
        summary="Original",
        target={"repository": "repo", "branch": "main", "profile": "db:1"},
        details={"acceptance": "Keep behavior"},
    )
    updated = TaskAgent(PartialModel()).extract(current, [])
    assert updated.target.repository == "repo"
    assert updated.target.profile == "db:1"
    assert updated.target.branch == "perf"
    assert updated.details == current.details
    assert current.target.branch == "main"


def test_tui_failed_open_keeps_the_previous_conversation():
    async def scenario():
        app = PerformanceChat("http://unused", "good")
        original = {"id": "good"}
        app.state = original

        class MissingApi:
            async def call(self, *args):
                raise ValueError("404")

        class Listener:
            cancelled = False

            def cancel(self):
                self.cancelled = True

        app.api = MissingApi()
        app.listener = Listener()
        try:
            await app.command("/open missing")
        except ValueError:
            pass
        assert app.cid == "good" and app.state is original
        assert not app.listener.cancelled

    asyncio.run(scenario())


def test_new_intake_uses_history_at_acceptance_not_later_executor_output(tmp_path):
    settings = Settings(db_path=str(tmp_path / "queue.sqlite3"))
    store = Store(settings.db_path)
    worker = Worker(settings)
    cid = store.create_conversation()["id"]
    task = PerformanceTask(summary="Original task", target={"repository": "repo", "branch": "main"})
    store.update_spec(cid, task, 1)
    rid = uuid4().hex
    store.start_run(cid, 2, rid)
    worker.tick()
    assert store.run(rid)["progress"]
    worker.tick()
    question = store.run(rid)["clarification"]
    store.resume(
        rid, ClarificationAnswer(request_id=question["request_id"], answer="No constraints")
    )
    worker.tick()
    store.message(cid, "New optimization task", uuid4().hex)
    worker.tick()  # The executor adds a result after the new user message.
    assert store.run(rid)["status"] == "demo"
    worker.tick()
    assert store.conversation(cid)["spec"]["summary"] == "New optimization task"


def test_failure_snapshot_survives_restart_and_is_cleared_by_new_request(tmp_path):
    path = str(tmp_path / "errors.sqlite3")
    store = Store(path)
    cid = store.create_conversation()["id"]
    store.message(cid, "test", uuid4().hex)
    store.fail(store.claim())
    assert Store(path).conversation(cid)["last_error"]["message"]
    store.message(cid, "retry", uuid4().hex)
    assert "last_error" not in store.conversation(cid)


def test_tui_retry_does_not_skip_event_when_snapshot_failed():
    async def scenario():
        ready = asyncio.Event()
        cursors = []

        class Harness(PerformanceChat):
            refreshes = 0

            async def refresh_state(self):
                self.refreshes += 1
                if self.refreshes == 2:
                    raise ValueError("Temporary GET failure")

            def query_one(self, *args):
                class Status:
                    def update(self, value):
                        pass

                return Status()

        class EventApi:
            async def events(self, cid, after):
                cursors.append(after)
                if len(cursors) == 1:
                    yield {"id": 7, "type": "execution.result"}
                else:
                    ready.set()
                    await asyncio.Future()

        app = Harness("http://unused", "conversation")
        app.api = EventApi()
        listener = asyncio.create_task(app.listen())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            assert cursors == [0, 0]
            assert app.refreshes == 3
        finally:
            listener.cancel()
            with suppress(asyncio.CancelledError):
                await listener

    asyncio.run(scenario())


def test_tui_numeric_profile_id_is_an_opaque_string():
    async def scenario():
        sent = []

        class FakeApi:
            async def call(self, method, path, body):
                sent.append(body)

        app = PerformanceChat("http://unused", "conversation")
        app.state = {"spec": PerformanceTask().model_dump(), "runs": []}
        app.api = FakeApi()
        await app.command("/set target.profile=42")
        assert sent[0]["spec"]["target"]["profile"] == "42"

    asyncio.run(scenario())
