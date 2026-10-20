import base64
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from perf_chat.api import create_app
from perf_chat.config import Settings
from perf_chat.contracts import PerformanceTask
from perf_chat.credentials import protect, unprotect
from perf_chat.integrations.gitlab_source import GitLabSourceGateway, object_hash
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent.source_context import prepare_source
from perf_chat.profiler_analysis_agent.source_contracts import SourceLocation, SourceRequest
from perf_chat.worker import Worker


@pytest.fixture(autouse=True)
def isolated_token_key(monkeypatch):
    # Tests never depend on or reuse a deployment's persistent encryption key.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PERF_CHAT_SECRET_KEY", Fernet.generate_key().decode())


def test_tokens_are_encrypted_and_not_returned_or_sent_to_models(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "tokens.db"),
        profiler_mcp_url="https://profiler.example/mcp",
        executor_mode="profile-mcp",
        git_mcp_url="https://git.example/mcp", git_mcp_token="synthetic-env-git-secret",
    )
    client = TestClient(create_app(settings))
    cid = client.post("/api/conversations").json()["id"]
    state = client.get(f"/api/conversations/{cid}").json()
    state["spec"]["summary"] = "Inspect"
    state["spec"]["target"].update(
        repository="https://gitlab.example/team/repo", branch="main", profile="id"
    )
    tokens = {"profiler_token": "synthetic-profiler-secret"}
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
    assert state["credentials"] == {"git_token_configured": False, "profiler_token_configured": True}
    rid = client.post(
        f"/api/conversations/{cid}/runs",
        json={"request_id": uuid4().hex, "spec_version": state["spec"]["spec_version"]},
    ).json()["id"]
    store = client.app.state.store
    run = store.run(rid)
    assert store.run_token(run, "git", run["task"]["target"]["repository"]) == ""
    assert store.run_token(run, "git", "https://other.example/team/repo") == ""
    executor = Worker(settings)._profile_executor(run)
    assert executor.agent.gateway.auth_headers() == {
        "Authorization": "Bearer " + tokens["profiler_token"]
    }
    assert executor.agent.git_gateway.auth_headers() == {
        "Authorization": "Bearer synthetic-env-git-secret"
    }
    assert "synthetic-env-git-secret" not in repr(settings)
    public = json.dumps(
        [
            state,
            client.get("/api/conversations").json(),
            client.get(f"/api/runs/{rid}").json(),
            store.events(cid, 0),
        ]
    )
    assert "synthetic-env-git-secret" not in public
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
    assert response.json()["credentials"]["profiler_token_configured"]
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
    assert store.run_token(run, "profiler", settings.profiler_mcp_url) == tokens["profiler_token"]


def test_failed_save_is_atomic_and_validation_does_not_echo_secrets(tmp_path):
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "atomic.db"),
                                          profiler_mcp_url="https://profiler.example/mcp")))
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
        json={"spec": state["spec"], "expected_version": 99, "credentials": {"profiler_token": token}},
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
    from analysis_helpers import drain_worker
    drain_worker(worker, rid)
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
                tools=[SimpleNamespace(name="read_sample", description="Read profile", inputSchema={"type": "object"}, outputSchema=None)], nextCursor=None
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
    gateway = MCPProfilerGateway("https://profiler.example/mcp", token="synthetic-mcp-token", allowed_tools=["read_sample"])
    assert gateway.call_tool(gateway.list_tools()[0], {"result_id": "id"}, result_id="id") == {"metrics": ["cycles"]}


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


def test_fernet_round_trip():
    # Exercise the non-Windows backend on every platform without changing os.name.
    from perf_chat.credentials import _fernet

    token = b"synthetic-portable-token"
    encrypted = _fernet().encrypt(token)
    assert token not in encrypted
    assert _fernet().decrypt(encrypted) == token


def test_legacy_git_credentials_can_be_cleared_but_never_sent_to_git_mcp(tmp_path):
    settings = Settings(db_path=str(tmp_path / "legacy.db"), executor_mode="profile-mcp",
                        git_mcp_url="https://git-tools.example/mcp",
                        git_mcp_token="synthetic-new-env-token")
    client = TestClient(create_app(settings))
    state = client.post("/api/conversations").json()
    cid = state["id"]
    state["spec"]["summary"] = "Inspect"
    state["spec"]["target"]["repository"] = "https://any-host.example/repository"
    store = client.app.state.store
    store.update_spec(cid, PerformanceTask.model_validate(state["spec"]), 1, {
        "git": ("synthetic-legacy-token", state["spec"]["target"]["repository"]),
    })
    rid = store.start_run(cid, 2, uuid4().hex)["id"]
    worker = Worker(settings)
    original = worker.store.run_token

    def no_legacy_token(run, kind, destination):
        assert kind != "git"
        return original(run, kind, destination)

    worker.store.run_token = no_legacy_token
    executor = worker._profile_executor(store.run(rid))
    assert executor.agent.git_gateway.auth_headers() == {
        "Authorization": "Bearer synthetic-new-env-token",
    }
    state = client.get(f"/api/conversations/{cid}").json()
    cleared = client.put(f"/api/conversations/{cid}/spec", json={
        "spec": state["spec"], "expected_version": 2, "credentials": {"git_token": ""},
    })
    assert cleared.status_code == 200 and not cleared.json()["credentials"]["git_token_configured"]


def test_git_mcp_personal_token_is_encrypted_pinned_and_bound_to_endpoint_and_repo(tmp_path):
    from dataclasses import replace

    settings = Settings(db_path=str(tmp_path / "personal.db"), executor_mode="profile-mcp",
                        git_mcp_url="https://git-tools.example/mcp",
                        git_mcp_token="synthetic-env-fallback")
    client = TestClient(create_app(settings))
    state = client.post("/api/conversations").json()
    cid = state["id"]
    state["spec"]["summary"] = "Inspect"
    state["spec"]["target"]["repository"] = "https://any-host.example/repo"

    def save(token=None):
        current = client.get(f"/api/conversations/{cid}").json()
        payload = {"spec": state["spec"], "expected_version": current["spec"]["spec_version"]}
        if token is not None:
            payload["credentials"] = {"git_token": token}
        response = client.put(f"/api/conversations/{cid}/spec", json=payload)
        assert response.status_code == 200
        return response.json()

    saved = save("synthetic-personal-original")
    assert saved["credentials"]["git_token_configured"]
    assert save()["credentials"]["git_token_configured"]
    store = client.app.state.store
    rid = store.start_run(cid, 3, uuid4().hex)["id"]
    run = store.run(rid)

    def headers(configuration=settings, record=run):
        return Worker(configuration)._profile_executor(record).agent.git_gateway.auth_headers()

    assert headers() == {"Authorization": "Bearer synthetic-personal-original"}
    rotated = save("synthetic-personal-replacement")
    assert rotated["credentials"]["git_token_configured"]
    assert headers() == {"Authorization": "Bearer synthetic-personal-original"}
    assert not save("")["credentials"]["git_token_configured"]
    assert headers() == {"Authorization": "Bearer synthetic-personal-original"}
    changed = replace(settings, git_mcp_url="https://other-server.example/mcp")
    assert headers(changed) == {"Authorization": "Bearer synthetic-env-fallback"}
    changed_run = json.loads(json.dumps(run))
    changed_run["task"]["target"]["repository"] = "https://other-host.example/repo"
    assert headers(record=changed_run) == {"Authorization": "Bearer synthetic-env-fallback"}
    public = json.dumps([saved, rotated, store.run(rid), store.events(cid, 0)])
    with store.tx() as db:
        ciphertext = " ".join(row[0] for row in db.execute("SELECT ciphertext FROM credentials"))
    for token in ["synthetic-personal-original", "synthetic-personal-replacement"]:
        assert token not in public and token not in ciphertext
    store.cancel(rid)
    Worker(settings).tick()
    assert store.run(rid)["status"] == "cancelled"
    with store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 0


@pytest.mark.parametrize("endpoint", ["", "http://remote.example/mcp"])
def test_personal_git_token_save_rejects_missing_or_insecure_mcp_endpoint(tmp_path, endpoint):
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "insecure.db"),
                                          git_mcp_url=endpoint)))
    state = client.post("/api/conversations").json()
    state["spec"]["target"]["repository"] = "https://host.example/repo"
    response = client.put(f"/api/conversations/{state['id']}/spec", json={
        "spec": state["spec"], "expected_version": 1,
        "credentials": {"git_token": "synthetic-personal-secret"},
    })
    assert response.status_code == 422
    assert "synthetic-personal-secret" not in response.text
