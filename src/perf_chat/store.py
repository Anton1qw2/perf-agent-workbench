import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from .contracts import ClarificationAnswer, PerformanceTask
from .credentials import protect, unprotect


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def uid():
    return uuid4().hex


class Store:
    def __init__(self, path: str):
        self.path = path
        with self.tx() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    lease_until REAL, token TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    type TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_cursor ON events(conversation_id, id);
                CREATE TABLE IF NOT EXISTS intake_turns (
                    job_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS intake_conversation ON intake_turns(conversation_id);
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, destination TEXT NOT NULL, ciphertext TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credential_bindings (
                    conversation_id TEXT NOT NULL, kind TEXT NOT NULL, credential_id TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, kind));
                CREATE TABLE IF NOT EXISTS active_run_credentials (
                    run_id TEXT NOT NULL, credential_id TEXT NOT NULL,
                    PRIMARY KEY(run_id, credential_id));
                INSERT OR IGNORE INTO active_run_credentials
                    SELECT id, json_extract(data, '$.credential_refs.git') FROM runs
                    WHERE json_extract(data, '$.status') IN ('queued','running','waiting','cancelling')
                    AND json_extract(data, '$.credential_refs.git') IS NOT NULL;
                INSERT OR IGNORE INTO active_run_credentials
                    SELECT id, json_extract(data, '$.credential_refs.profiler') FROM runs
                    WHERE json_extract(data, '$.status') IN ('queued','running','waiting','cancelling')
                    AND json_extract(data, '$.credential_refs.profiler') IS NOT NULL;
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, db, table, key):
        row = db.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFound(key)
        return json.loads(row[0])

    def _save(self, db, table, data):
        db.execute(f"UPDATE {table} SET data=? WHERE id=?", (json.dumps(data), data["id"]))

    def _event(self, db, cid, kind, payload):
        db.execute(
            "INSERT INTO events(conversation_id,type,payload) VALUES(?,?,?)",
            (
                cid,
                kind,
                json.dumps(payload),
            ),
        )

    def _prune_credentials(self, db):
        db.execute(
            "DELETE FROM credentials WHERE id NOT IN (SELECT credential_id FROM credential_bindings) "
            "AND id NOT IN (SELECT credential_id FROM active_run_credentials)"
        )

    def _release_run_credentials(self, db, rid):
        db.execute("DELETE FROM active_run_credentials WHERE run_id=?", (rid,))
        self._prune_credentials(db)

    def _busy(self, db, cid):
        return (
            db.execute(
                "SELECT 1 FROM jobs WHERE conversation_id=? AND kind='intake' "
                "AND status IN ('queued','processing')",
                (cid,),
            ).fetchone()
            is not None
        )

    def _enqueue(self, db, cid, kind, payload, request_id):
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?, 'queued',NULL,NULL,?)",
            (
                request_id,
                cid,
                kind,
                json.dumps(payload),
                time.time(),
            ),
        )

    def _replay(self, db, request_id, cid, kind, payload):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (request_id,)).fetchone()
        if row:
            previous = json.loads(row["payload"])
            if kind == "intake":
                previous = {"content": previous.get("content")}
            if row["conversation_id"] != cid or row["kind"] != kind or previous != payload:
                raise Conflict("request_id has already been used for another command")
            return True
        return False

    def create_conversation(self):
        cid = uid()
        value = {"id": cid, "messages": [], "spec": PerformanceTask(task_id=uid()).model_dump()}
        with self.tx() as db:
            db.execute("INSERT INTO conversations VALUES(?,?)", (cid, json.dumps(value)))
            self._event(db, cid, "conversation.created", {})
        return value

    def list_conversations(self):
        with self.tx() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT json_object('id', json_extract(data, '$.id'), "
                    "'spec', json_object('summary', json_extract(data, '$.spec.summary'))) "
                    "FROM conversations ORDER BY rowid DESC LIMIT 100",
                )
            ]

    def conversation(self, cid):
        with self.tx() as db:
            value = self._get(db, "conversations", cid)
            value["busy"] = self._busy(db, cid)
            value["runs"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT json_object('id', json_extract(data, '$.id'), "
                    "'status', json_extract(data, '$.status'), "
                    "'clarification', json_extract(data, '$.clarification'), "
                    "'result', json_extract(data, '$.result'), "
                    "'progress', json_extract(data, '$.progress')) "
                    "FROM runs WHERE conversation_id=? ORDER BY rowid DESC",
                    (cid,),
                )
            ]
            value["missing_fields"] = PerformanceTask.model_validate(value["spec"]).missing_fields()
            return value

    def message(self, cid, content, request_id):
        payload = {"content": content}
        with self.tx() as db:
            conv = self._get(db, "conversations", cid)
            if self._replay(db, request_id, cid, "intake", payload):
                return
            if self._busy(db, cid):
                raise Conflict("Wait for the agent to reply")
            conv["messages"].append({"role": "user", "content": content})
            conv.pop("last_error", None)
            self._save(db, "conversations", conv)
            self._enqueue(
                db,
                cid,
                "intake",
                {
                    **payload,
                    "message_count": len(conv["messages"]),
                },
                request_id,
            )
            self._event(db, cid, "message.accepted", {"request_id": request_id})

    def update_spec(self, cid, spec, expected_version, credentials=None):
        with self.tx() as db:
            conv = self._get(db, "conversations", cid)
            if self._busy(db, cid) or conv["spec"]["spec_version"] != expected_version:
                raise Conflict("The task changed or the agent is busy; refresh the data")
            spec.task_id = conv["spec"]["task_id"]
            spec.spec_version = expected_version + 1
            conv["spec"] = spec.model_dump()
            self._save(db, "conversations", conv)
            for kind, entry in (credentials or {}).items():
                token, destination = entry
                db.execute(
                    "DELETE FROM credential_bindings WHERE conversation_id=? AND kind=?",
                    (cid, kind),
                )
                if token:
                    credential_id = uid()
                    db.execute(
                        "INSERT INTO credentials VALUES(?,?,?)",
                        (credential_id, destination, protect(token)),
                    )
                    db.execute(
                        "INSERT INTO credential_bindings VALUES(?,?,?)", (cid, kind, credential_id)
                    )
            if credentials:
                self._prune_credentials(db)
            self._event(db, cid, "spec.updated", {"version": spec.spec_version})

    def start_run(self, cid, version, request_id):
        payload = {"version": version}
        with self.tx() as db:
            conv = self._get(db, "conversations", cid)
            if self._replay(db, request_id, cid, "execute", payload):
                return self._get(db, "runs", request_id)
            if self._busy(db, cid) or version != conv["spec"]["spec_version"]:
                raise Conflict("Refresh the task and wait for the agent to reply")
            task = PerformanceTask.model_validate(conv["spec"])
            if task.missing_fields():
                raise Conflict("Missing fields: " + ", ".join(task.missing_fields()))
            active = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM runs WHERE conversation_id=?",
                    (cid,),
                )
            ]
            if any(r["status"] in {"queued", "running", "waiting", "cancelling"} for r in active):
                raise Conflict("This chat already has an active run")
            run = {
                "id": request_id,
                "conversation_id": cid,
                "status": "queued",
                "task": task.model_dump(),
                "cursor": {},
                "clarification": None,
                "answer": None,
                "result": None,
                "steps": 0,
                "runtime_seconds": 0,
                "progress": "",
                "credential_refs": {
                    row["kind"]: row["credential_id"]
                    for row in db.execute(
                        "SELECT b.kind, b.credential_id FROM credential_bindings b "
                        "JOIN credentials c ON c.id=b.credential_id WHERE b.conversation_id=? "
                        "AND (b.kind!='git' OR c.destination=?)",
                        (cid, task.target.repository or ""),
                    )
                },
            }
            db.execute("INSERT INTO runs VALUES(?,?,?)", (request_id, cid, json.dumps(run)))
            for credential_id in run["credential_refs"].values():
                db.execute(
                    "INSERT INTO active_run_credentials VALUES(?,?)", (request_id, credential_id)
                )
            conv.pop("last_error", None)
            self._save(db, "conversations", conv)
            self._enqueue(db, cid, "execute", payload, request_id)
            self._event(db, cid, "run.queued", {"run_id": request_id})
            return run

    def run(self, rid):
        with self.tx() as db:
            return self._get(db, "runs", rid)

    def credential_status(self, cid, repository, profiler_url, *, git_destination=None):
        with self.tx() as db:
            rows = db.execute(
                "SELECT b.kind, c.destination FROM credential_bindings b "
                "JOIN credentials c ON c.id=b.credential_id WHERE b.conversation_id=?",
                (cid,),
            ).fetchall()
            return {
                "git_token_configured": any(
                    (r["kind"] == "git_mcp" and r["destination"] == git_destination
                     if git_destination is not None
                     else r["kind"] == "git" and r["destination"] == repository) for r in rows
                ),
                "profiler_token_configured": any(
                    r["kind"] == "profiler" and r["destination"] == profiler_url for r in rows
                ),
            }

    def run_token(self, run, kind, destination):
        credential_id = run.get("credential_refs", {}).get(kind)
        if not credential_id:
            return ""
        with self.tx() as db:
            row = db.execute(
                "SELECT ciphertext FROM credentials WHERE id=? AND destination=?",
                (credential_id, destination),
            ).fetchone()
            return unprotect(row[0]) if row else ""

    def resume(self, rid, answer: ClarificationAnswer):
        with self.tx() as db:
            run = self._get(db, "runs", rid)
            if run["status"] != "waiting":
                raise Conflict("The run is not awaiting a reply")
            if run["clarification"]["request_id"] != answer.request_id:
                raise Conflict("The answer belongs to a different clarification")
            run.update(status="queued", answer=answer.model_dump(), clarification=None)
            self._save(db, "runs", run)
            conv = self._get(db, "conversations", run["conversation_id"])
            conv["messages"].append({"role": "user", "content": answer.answer})
            self._save(db, "conversations", conv)
            self._enqueue(db, run["conversation_id"], "step", {"run_id": rid}, uid())
            self._event(db, run["conversation_id"], "run.resumed", {"run_id": rid})

    def cancel(self, rid):
        with self.tx() as db:
            run = self._get(db, "runs", rid)
            if run["status"] not in {"queued", "running", "waiting", "cancelling"}:
                return
            was_waiting = run["status"] == "waiting"
            run["status"] = "cancelling"
            self._save(db, "runs", run)
            if was_waiting:
                self._enqueue(db, run["conversation_id"], "step", {"run_id": rid}, uid())
            self._event(db, run["conversation_id"], "run.cancelling", {"run_id": rid})

    def events(self, cid, after):
        with self.tx() as db:
            return [
                dict(id=row["id"], type=row["type"], payload=json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT * FROM events WHERE conversation_id=? AND id>? ORDER BY id LIMIT 200",
                    (cid, after),
                )
            ]

    def claim(self):
        with self.tx() as db:
            now = time.time()
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' OR "
                "(status='processing' AND lease_until<?) ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            job["token"] = uid()
            db.execute(
                "UPDATE jobs SET status='processing',lease_until=?,token=? WHERE id=?",
                (
                    now + 600,
                    job["token"],
                    job["id"],
                ),
            )
            job["payload"] = json.loads(job["payload"])
            return job

    def _owns(self, db, job):
        return (
            db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND token=? AND status='processing'",
                (
                    job["id"],
                    job["token"],
                ),
            ).fetchone()
            is not None
        )

    def intake_context(self, conversation_id):
        with self.tx() as db:
            row = db.execute(
                "SELECT data FROM intake_turns WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            return json.loads(row[0]) if row else {}

    def task_messages(self, conversation_id, messages):
        """Consultation and capability turns stay in chat, outside later task input."""
        with self.tx() as db:
            rows = db.execute(
                "SELECT json_extract(data, '$.route.intent'), "
                "json_extract(data, '$.message_indices'), json_extract(data, '$.task_reply') "
                "FROM intake_turns WHERE conversation_id=?",
                (conversation_id,),
            ).fetchall()
        excluded, replacements = set(), {}
        for intent, indices, task_reply in rows:
            indices = json.loads(indices or "[]")
            if intent in {"consultation", "capabilities"}:
                excluded.update(indices)
            elif intent == "mixed" and indices and task_reply is not None:
                replacements[indices[-1]] = task_reply
        return [{**message, "content": replacements.get(index, message["content"])}
                for index, message in enumerate(messages) if index not in excluded]

    def finish_intake(self, job, output):
        with self.tx() as db:
            if not self._owns(db, job):
                return
            conv = self._get(db, "conversations", job["conversation_id"])
            route = output.get("route", {"intent": "task"})
            if route["intent"] not in {"consultation", "capabilities"}:
                task = PerformanceTask.model_validate(output["task"])
                task.spec_version = conv["spec"]["spec_version"] + 1
                conv["spec"] = task.model_dump()
            db.execute("INSERT INTO intake_turns VALUES(?,?,?)", (
                job["id"], conv["id"], json.dumps({
                    "route": route, "consultation": output.get("consultation"),
                    "pending_expert": output.get("pending_expert"),
                    "task_reply": output.get("task_reply"),
                    "message_indices": [job["payload"].get("message_count", len(conv["messages"])) - 1,
                                        len(conv["messages"])],
                }),
            ))
            conv["messages"].append({"role": "assistant", "content": output["reply"]})
            self._save(db, "conversations", conv)
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
            self._event(db, conv["id"], "intake.completed", {"version": conv["spec"]["spec_version"]})

    def finish_step(self, job, rid, event, cursor, elapsed):
        with self.tx() as db:
            if not self._owns(db, job):
                return
            run = self._get(db, "runs", rid)
            cid = run["conversation_id"]
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
            if run["status"] == "cancelling":
                # Another leased step will perform cancellation, even if this call completed.
                self._enqueue(db, cid, "step", {"run_id": rid}, uid())
                return
            run.update(
                cursor=cursor,
                answer=None,
                steps=run["steps"] + 1,
                runtime_seconds=run["runtime_seconds"] + elapsed,
            )
            if event.type == "clarification":
                run.update(status="waiting", clarification=event.clarification.model_dump())
                conv = self._get(db, "conversations", cid)
                conv["messages"].append(
                    {
                        "role": "executor",
                        "content": event.clarification.question,
                    }
                )
                self._save(db, "conversations", conv)
            elif event.type == "result":
                if event.result.task_id != run["task"]["task_id"]:
                    raise ValueError("Executor returned result for another task")
                run.update(status=event.result.status, result=event.result.model_dump())
                conv = self._get(db, "conversations", cid)
                conv["messages"].append({"role": "assistant", "content": event.result.summary})
                self._save(db, "conversations", conv)
            else:
                run["status"] = "running"
                run["progress"] = event.message
                self._enqueue(db, cid, "step", {"run_id": rid}, uid())
            self._save(db, "runs", run)
            if event.type == "result":
                self._release_run_credentials(db, rid)
            self._event(
                db,
                cid,
                "execution." + event.type,
                {
                    "run_id": rid,
                    **event.model_dump(),
                },
            )

    def finish_cancel(self, job, rid):
        with self.tx() as db:
            if not self._owns(db, job):
                return
            run = self._get(db, "runs", rid)
            run["status"] = "cancelled"
            self._save(db, "runs", run)
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
            self._event(db, run["conversation_id"], "run.cancelled", {"run_id": rid})
            self._release_run_credentials(db, rid)

    def fail(self, job):
        # Details stay in worker logs; never send provider responses/credentials to clients.
        with self.tx() as db:
            if not self._owns(db, job):
                return
            db.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
            error = {
                "job_id": job["id"],
                "message": "Processing failed. Check the settings and worker logs.",
            }
            conv = self._get(db, "conversations", job["conversation_id"])
            conv["last_error"] = error
            self._save(db, "conversations", conv)
            if job["kind"] != "intake":
                rid = job["id"] if job["kind"] == "execute" else job["payload"]["run_id"]
                run = self._get(db, "runs", rid)
                run["status"] = "failed"
                self._save(db, "runs", run)
                self._release_run_credentials(db, rid)
            self._event(
                db,
                job["conversation_id"],
                "job.failed",
                error,
            )
