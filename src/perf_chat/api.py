import asyncio
import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from .config import Settings
from .contracts import ClarificationAnswer, PerformanceTask
from .integrations.gitlab_source import repository_url
from .integrations.profiler_mcp import validate_token_destination
from .store import Conflict, NotFound, Store


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    request_id: str = Field(min_length=8, max_length=100)


class CredentialInput(BaseModel):
    git_token: SecretStr | None = None
    profiler_token: SecretStr | None = None


class SpecInput(BaseModel):
    spec: PerformanceTask
    expected_version: int = Field(ge=1)
    credentials: CredentialInput | None = None


class RunInput(BaseModel):
    spec_version: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    app = FastAPI(title="Performance Chat", version="0.1.0")
    app.state.store = store

    def snapshot(cid):
        value = store.conversation(cid)
        value["credentials"] = store.credential_status(
            cid,
            value["spec"]["target"].get("repository"),
            settings.profiler_mcp_url,
        )
        return value

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, exc):
        # Validation input can contain secrets; do not echo it into responses.
        return JSONResponse(status_code=422, content={"detail": "Invalid request fields"})

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NotFound)
    async def not_found(_, exc):
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": settings.mode, "executor": settings.executor_mode}

    @app.post("/api/conversations", status_code=201)
    def create_conversation():
        return store.create_conversation()

    @app.get("/api/conversations")
    def conversations():
        return store.list_conversations()

    @app.get("/api/conversations/{cid}")
    def conversation(cid: str):
        return snapshot(cid)

    @app.post("/api/conversations/{cid}/messages", status_code=202)
    def message(cid: str, body: MessageInput):
        store.message(cid, body.content, body.request_id)
        return {"job_id": body.request_id}

    @app.put("/api/conversations/{cid}/spec")
    def spec(cid: str, body: SpecInput):
        if (body.spec.target.repository or "").startswith("https://"):
            try:
                repository_url(body.spec.target.repository)
            except ValueError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "Use an HTTPS repository URL without embedded credentials or query parameters"
                    },
                )
        changes = {}
        for field, kind, destination in [
            ("git_token", "git", body.spec.target.repository or ""),
            ("profiler_token", "profiler", settings.profiler_mcp_url),
        ]:
            secret = getattr(body.credentials, field, None)
            if secret is not None:
                value = secret.get_secret_value()
                if len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                    return JSONResponse(status_code=422, content={"detail": "Invalid token format"})
                if value and kind == "profiler" and not destination:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": "Configure PROFILER_MCP_URL before saving its token"},
                    )
                if value and kind == "profiler":
                    try:
                        validate_token_destination(destination)
                    except ValueError:
                        return JSONResponse(status_code=422, content={
                            "detail": "Profiler tokens require HTTPS or loopback HTTP"
                        })
                if value and kind == "git":
                    try:
                        repository_url(destination)
                    except ValueError:
                        return JSONResponse(
                            status_code=422,
                            content={
                                "detail": "Use an HTTPS GitLab repository URL without embedded credentials"
                            },
                        )
                changes[kind] = (value, destination)
        try:
            store.update_spec(cid, body.spec, body.expected_version, changes)
        except ValueError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Encrypted token storage is unavailable; configure backend secret storage"
                },
            )
        return snapshot(cid)

    @app.post("/api/conversations/{cid}/runs", status_code=202)
    def start_run(cid: str, body: RunInput):
        return store.start_run(cid, body.spec_version, body.request_id)

    @app.get("/api/runs/{rid}")
    def run(rid: str):
        return store.run(rid)

    @app.post("/api/runs/{rid}/resume", status_code=202)
    def resume(rid: str, body: ClarificationAnswer):
        store.resume(rid, body)
        return store.run(rid)

    @app.post("/api/runs/{rid}/cancel", status_code=202)
    def cancel(rid: str):
        store.cancel(rid)
        return store.run(rid)

    @app.get("/api/conversations/{cid}/events")
    async def events(
        cid: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        store.conversation(cid)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Last-Event-ID"})

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(store.events, cid, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event)}\n\n"
                if not batch:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "ui" / "web", html=True))
    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
