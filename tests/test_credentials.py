import base64
import hashlib
import json
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from perf_chat.api import create_app
from perf_chat.config import Settings
from perf_chat.credentials import protect, unprotect
from perf_chat.integrations.gitlab_source import GitLabSourceGateway, object_hash
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent.source_context import prepare_source
from perf_chat.profiler_analysis_agent.source_contracts import SourceLocation, SourceRequest
from perf_chat.worker import Worker


def test_tokens_are_encrypted_and_not_returned_or_sent_to_models(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "tokens.db"),
        profiler_mcp_url="https://profiler.example/mcp",
        executor_mode="profile-mcp",
    )
    client = TestClient(create_app(settings))
    cid = client.post("/api/conversations").json()["id"]
    state = client.get(f"/api/conversations/{cid}").json()
    state["spec"]["summary"] = "Inspect"
    state["spec"]["target"].update(
        repository="https://gitlab.example/team/repo", branch="main", profile="id"
    )
    tokens = {"git_token": "synthetic-git-secret", "profiler_token": "synthetic-profiler-secret"}
    response = client.put(
        f"/api/conversations/{cid}/spec",
        json={
            "spec": state["spec"],
            "expected_version": state["spec"]["spec_version"],
            "credentials": tokens,
        },
    )
    assert response.status_code == 200
    state = response.json()
    assert state["credentials"] == {"git_token_configured": True, "profiler_token_configured": True}
    rid = client.post(
        f"/api/conversations/{cid}/runs",
        json={"request_id": uuid4().hex, "spec_version": state["spec"]["spec_version"]},
    ).json()["id"]
    store = client.app.state.store
    run = store.run(rid)
    assert store.run_token(run, "git", run["task"]["target"]["repository"]) == tokens["git_token"]
    assert store.run_token(run, "git", "https://other.example/team/repo") == ""
    executor = Worker(settings)._profile_executor(run)
    assert executor.agent.gateway.auth_headers() == {
        "Authorization": "Bearer " + tokens["profiler_token"]
    }
    assert executor.agent.source_gateway.resolve_revision.__self__._token == tokens["git_token"]
    public = json.dumps(
        [
            state,
            client.get("/api/conversations").json(),
            client.get(f"/api/runs/{rid}").json(),
            store.events(cid, 0),
        ]
    )
    with store.tx() as db:
        ciphertext = " ".join(row[0] for row in db.execute("SELECT ciphertext FROM credentials"))
    for token in tokens.values():
        assert token not in public and token not in ciphertext

    # Metadata-only updates preserve tokens. Empty strings explicitly clear bindings.
    state = client.get(f"/api/conversations/{cid}").json()
    response = client.put(
        f"/api/conversations/{cid}/spec",
        json={"spec": state["spec"], "expected_version": state["spec"]["spec_version"]},
    )
    assert response.json()["credentials"]["git_token_configured"]
    state = response.json()
    response = client.put(
        f"/api/conversations/{cid}/spec",
        json={
            "spec": state["spec"],
            "expected_version": state["spec"]["spec_version"],
            "credentials": {"git_token": "", "profiler_token": ""},
        },
    )
    assert not any(response.json()["credentials"].values())
    assert store.run_token(run, "git", run["task"]["target"]["repository"]) == tokens["git_token"]


def test_failed_save_is_atomic_and_validation_does_not_echo_secrets(tmp_path):
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "atomic.db"))))
    cid = client.post("/api/conversations").json()["id"]
    state = client.get(f"/api/conversations/{cid}").json()
    token = "synthetic-invalid-secret"
    response = client.put(
        f"/api/conversations/{cid}/spec",
        json={
            "spec": state["spec"],
            "expected_version": token,
            "credentials": {"git_token": token},
        },
    )
    assert response.status_code == 422 and token not in response.text
    assert not any(client.get(f"/api/conversations/{cid}").json()["credentials"].values())
    state["spec"]["target"]["repository"] = "https://gitlab.example/team/repo"
    response = client.put(
        f"/api/conversations/{cid}/spec",
        json={"spec": state["spec"], "expected_version": 99, "credentials": {"git_token": token}},
    )
    assert response.status_code == 409
    assert not any(client.get(f"/api/conversations/{cid}").json()["credentials"].values())


def test_real_encryption_round_trip():
    token = "synthetic-encryption-test"
    encoded = protect(token)
    assert token not in encoded and unprotect(encoded) == token


def test_gitlab_api_uses_header_pinned_objects_and_no_redirects():
    raw = b"int hot() {\n  return 1;\n}\n"
    blob = object_hash("blob", raw, "0" * 40)
    tree_data = b"100644 hot.c\x00" + bytes.fromhex(blob)
    tree = object_hash("tree", tree_data, "0" * 40)
    commit = "a" * 40
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.headers["PRIVATE-TOKEN"] == "synthetic-gitlab-token"
        assert "synthetic-gitlab-token" not in str(request.url)
        path = request.url.path
        if "/repository/commits/" in path:
            return httpx.Response(200, json={"id": commit})
        if path.endswith("/repository/tree"):
            assert request.url.params["ref"] == commit
            return httpx.Response(
                200,
                json=[
                    {"name": "hot.c", "path": "hot.c", "type": "blob", "mode": "100644", "id": blob}
                ],
            )
        if "/repository/files/" in path:
            assert request.method == "HEAD"
            return httpx.Response(
                200, headers={"X-Gitlab-Size": str(len(raw)), "X-Gitlab-Blob-Id": blob}
            )
        if "/repository/blobs/" in path:
            return httpx.Response(
                200,
                json={
                    "size": len(raw),
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode(),
                },
            )
        raise AssertionError(path)

    repository = "https://gitlab.example/group/project.git"
    gateway = GitLabSourceGateway(
        repository, "synthetic-gitlab-token", transport=httpx.MockTransport(handler)
    )
    snapshot = prepare_source(gateway, SourceRequest(repository=repository, ref="main"))
    assert snapshot.commit == commit and snapshot.tree == tree
    item = gateway.read_source(
        repository, commit, SourceLocation(path="hot.c", start_line=1, end_line=3)
    )
    assert item.text == raw.decode() and item.sha256 == hashlib.sha256(raw).hexdigest()
    assert all(request.url.host == "gitlab.example" for request in calls)
    redirects = []

    async def redirect(request):
        redirects.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/steal"})

    gateway = GitLabSourceGateway(
        repository, "synthetic-gitlab-token", transport=httpx.MockTransport(redirect)
    )
    assert (
        prepare_source(gateway, SourceRequest(repository=repository, ref="main")).status
        == "unavailable"
    )
    assert len(redirects) == 1


def test_cleared_tokens_survive_only_until_active_run_finishes(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "prune.db"),
        executor_mode="profile-demo",
        profiler_mcp_url="https://profiler.example/mcp",
    )
    client = TestClient(create_app(settings))
    cid = client.post("/api/conversations").json()["id"]
    state = client.get(f"/api/conversations/{cid}").json()
    state["spec"]["summary"] = "Inspect"
    state["spec"]["target"].update(
        repository="https://gitlab.example/team/repo", branch="main", profile="id"
    )
    saved = client.put(
        f"/api/conversations/{cid}/spec",
        json={
            "spec": state["spec"],
            "expected_version": 1,
            "credentials": {"profiler_token": "synthetic-retained-token"},
        },
    ).json()
    rid = client.post(
        f"/api/conversations/{cid}/runs",
        json={"request_id": uuid4().hex, "spec_version": saved["spec"]["spec_version"]},
    ).json()["id"]
    client.put(
        f"/api/conversations/{cid}/spec",
        json={
            "spec": saved["spec"],
            "expected_version": saved["spec"]["spec_version"],
            "credentials": {"profiler_token": ""},
        },
    )
    store = client.app.state.store
    assert store.run_token(store.run(rid), "profiler", settings.profiler_mcp_url)
    worker = Worker(settings)
    worker.tick()
    worker.tick()
    assert store.run(rid)["status"] == "demo"
    assert store.run_token(store.run(rid), "profiler", settings.profiler_mcp_url) == ""


def test_mcp_transport_receives_token_only_as_header(monkeypatch):
    import sys
    from contextlib import asynccontextmanager
    from types import ModuleType, SimpleNamespace

    mcp, client_module, transport_module = (
        ModuleType("mcp"),
        ModuleType("mcp.client"),
        ModuleType("mcp.client.streamable_http"),
    )

    @asynccontextmanager
    async def transport(url, *, http_client):
        assert url == "https://profiler.example/mcp"
        assert http_client.headers["Authorization"] == "Bearer synthetic-mcp-token"
        assert http_client.follow_redirects is False
        yield object(), object(), lambda: None

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def list_tools(self, **kwargs):
            return SimpleNamespace(
                tools=[SimpleNamespace(name="get_list_metrics")], nextCursor=None
            )

        async def call_tool(self, name, arguments):
            assert arguments == {"result_id": "id"}
            return SimpleNamespace(
                isError=False, structuredContent={"metrics": ["cycles"]}, content=[]
            )

    mcp.ClientSession = Session
    transport_module.streamable_http_client = transport
    for name, module in [
        ("mcp", mcp),
        ("mcp.client", client_module),
        ("mcp.client.streamable_http", transport_module),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
    gateway = MCPProfilerGateway("https://profiler.example/mcp", token="synthetic-mcp-token")
    assert gateway.get_list_metrics("id") == {"metrics": ["cycles"]}


def test_profiler_token_requires_secure_destination(tmp_path):
    import pytest

    for url in ["http://remote.example/mcp", "https://user:pass@example.com/mcp"]:
        with pytest.raises(ValueError):
            MCPProfilerGateway(url, token="synthetic").auth_headers()
    for url in ["https://profiler.example/mcp", "http://127.0.0.1/mcp", "http://[::1]/mcp"]:
        assert MCPProfilerGateway(url, token="synthetic").auth_headers()
    client = TestClient(create_app(Settings(
        db_path=str(tmp_path / "http.db"), profiler_mcp_url="http://remote.example/mcp"
    )))
    state = client.post("/api/conversations").json()
    response = client.put(f"/api/conversations/{state['id']}/spec", json={
        "spec": state["spec"], "expected_version": 1,
        "credentials": {"profiler_token": "synthetic"},
    })
    assert response.status_code == 422


def test_gitlab_search_skips_unsupported_files_but_propagates_access_errors(monkeypatch):
    import pytest

    from perf_chat.integrations.gitlab_source import SourceUnavailable, UnsupportedSource

    gateway = GitLabSourceGateway("https://gitlab.example/group/repo")
    monkeypatch.setattr(gateway, "_tree", lambda *args, **kwargs: [
        {"mode": "100644", "path": "logo.png"},
        {"mode": "100644", "path": "hot.c"},
    ])
    def blob(repository, commit, path):
        if path == "logo.png":
            raise UnsupportedSource("Binary")
        return "a" * 40, "int hot() {}"
    monkeypatch.setattr(gateway, "_blob", blob)
    assert gateway.find_source(gateway.repository, "a" * 40, "hot")[0].path == "hot.c"
    def inaccessible(*args):
        raise SourceUnavailable("Access denied")
    monkeypatch.setattr(gateway, "_blob", inaccessible)
    with pytest.raises(SourceUnavailable, match="Access denied"):
        gateway.find_source(gateway.repository, "a" * 40, "hot")
