import hashlib
import json
import os
import subprocess
import sys

import pytest

from perf_chat.contracts import ClarificationAnswer, PerformanceTask
from perf_chat.integrations.git_source import GitSourceGateway, SourceUnavailable
from perf_chat.integrations.profiler_mcp import DemoProfilerGateway
from perf_chat.profiler_analysis_agent import AnalysisRequest, ProfileAnalysisAgent
from perf_chat.profiler_analysis_agent.context_builder import (
    ContextBudgetExceeded,
    PromptContextBuilder,
)
from perf_chat.profiler_analysis_agent.executor import ProfileAnalysisExecutor
from perf_chat.profiler_analysis_agent.source_context import prepare_source
from perf_chat.profiler_analysis_agent.source_contracts import SourceLocation, SourceRequest


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args, input=None):
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-C",
                str(root),
                *args,
            ],
            input=input,
            capture_output=True,
            check=True,
            env=env,
        ).stdout

    git("init", "-b", "main")
    (root / "hot.c").write_text("int hot() {\n  return 1;\n}\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "initial")
    return root, git, GitSourceGateway({"repo": str(root)})


def source(**kwargs):
    return SourceRequest(
        repository="repo",
        ref="main",
        locations=[
            SourceLocation(path="hot.c", start_line=1, end_line=3),
        ],
        **kwargs,
    )


def test_reads_pinned_blob_without_touching_worktree(repository):
    root, git, gateway = repository
    snapshot = prepare_source(gateway, source())
    (root / "hot.c").write_text("int hot() { return 2; }\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "new head")
    before = git("status", "--porcelain")
    item = gateway.read_source("repo", snapshot.commit, source().locations[0])
    assert "return 1" in item.text and "return 2" not in item.text
    assert item.sha256 == hashlib.sha256(item.text.encode()).hexdigest()
    assert git("status", "--porcelain") == before
    assert git("rev-parse", "HEAD").decode().strip() != snapshot.commit


@pytest.mark.parametrize(
    "path", ["../secret", "/tmp/secret", "C:/secret", "sub\\file", ".git/config"]
)
def test_rejects_non_tree_paths(repository, path):
    _, _, gateway = repository
    snapshot = prepare_source(gateway, source())
    with pytest.raises(SourceUnavailable):
        gateway.read_source("repo", snapshot.commit, SourceLocation(path=path))


def test_symlinks_gitlinks_lfs_binary_and_large_blobs_are_not_followed(repository):
    root, git, gateway = repository
    (root / "binary").write_bytes(b"a\x00b")
    (root / "large").write_bytes(b"x" * 262145)
    (root / "pointer").write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
    git("add", "binary", "large", "pointer")
    link_blob = git("hash-object", "-w", "--stdin", input=b"../outside").decode().strip()
    commit = git("rev-parse", "HEAD").decode().strip()
    git("update-index", "--add", "--cacheinfo", f"120000,{link_blob},link")
    git("update-index", "--add", "--cacheinfo", f"160000,{commit},submodule")
    git("commit", "-m", "unsupported objects")
    snapshot = prepare_source(gateway, source())
    for path in ["binary", "large", "pointer", "link", "submodule", "missing"]:
        with pytest.raises(SourceUnavailable):
            gateway.read_source("repo", snapshot.commit, SourceLocation(path=path))


def test_option_like_ref_and_unregistered_repository_rejected(repository):
    _, _, gateway = repository
    assert (
        prepare_source(gateway, SourceRequest(repository="repo", ref="--help")).status
        == "unavailable"
    )
    assert (
        prepare_source(gateway, SourceRequest(repository="unregistered", ref="main")).status
        == "unavailable"
    )


def test_stdout_is_bounded_and_timeout_reaps_process(repository, monkeypatch):
    root, git, gateway = repository
    with pytest.raises(SourceUnavailable, match="output exceeds"):
        gateway._run("repo", ["rev-parse", "HEAD"], limit=2)
    original = subprocess.Popen
    children = []

    def slow(*args, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr("perf_chat.integrations.git_source.subprocess.Popen", slow)
    gateway.timeout = 0.05
    with pytest.raises(SourceUnavailable, match="time budget"):
        gateway._run("repo", ["rev-parse", "HEAD"])
    assert children[0].poll() is not None


class SourceModel:
    def __init__(self, cite_omitted=False):
        self.payload = None
        self.cite_omitted = cite_omitted

    def complete(self, system, payload, **kwargs):
        self.payload = payload
        assert "untrusted" in system
        source_ids = [item["id"] for item in payload["source_evidence"]]
        return json.dumps(
            {
                "observations": [
                    {
                        "text": "Selected code returns 1.",
                        "evidence_ids": source_ids
                        or (["src_omitted"] if self.cite_omitted else ["e2"]),
                    }
                ]
            }
        )


def test_model_receives_source_and_unified_citations(repository):
    _, _, gateway = repository
    model = SourceModel()
    report = ProfileAnalysisAgent(DemoProfilerGateway(), model, source_gateway=gateway).analyze(
        AnalysisRequest(result_id="id", goal="Inspect", source=source(policy="required")),
    )
    assert report.status == "completed"
    assert report.source_context.required_delivery == "delivered"
    item = report.source_context.evidence[0]
    assert item.id in report.delivered_evidence_ids
    assert model.payload["source_evidence"][0]["text"] == item.text
    assert report.source_context.snapshot.profile_revision_match == "unverified"


def test_optional_source_failure_keeps_metrics_and_required_asks(repository):
    _, _, gateway = repository
    missing = SourceRequest(
        repository="repo",
        ref="main",
        policy="optional",
        locations=[SourceLocation(path="missing.c")],
    )
    model = SourceModel()
    agent = ProfileAnalysisAgent(DemoProfilerGateway(), model, source_gateway=gateway)
    report = agent.analyze(AnalysisRequest(result_id="id", goal="Inspect", source=missing))
    assert report.status == "completed" and len(report.evidence) == 2
    missing.policy = "required"
    report = agent.analyze(AnalysisRequest(result_id="id", goal="Inspect", source=missing))
    assert report.status == "needs_input" and report.clarification_field == "source"


def test_single_symbol_match_remains_candidate_and_demo_does_not_loop(repository):
    _, _, gateway = repository
    selection = SourceRequest(repository="repo", ref="main", symbols=["int hot"])
    agent = ProfileAnalysisAgent(DemoProfilerGateway(), source_gateway=gateway)
    report = agent.analyze(AnalysisRequest(result_id="id", goal="Inspect", source=selection))
    assert report.source_context.evidence[0].resolution == "text_candidate"
    selection.policy = "required"
    report = agent.analyze(AnalysisRequest(result_id="id", goal="Inspect", source=selection))
    assert report.status == "completed"
    assert report.source_context.required_delivery == "interpretation_disabled"


def test_source_snapshot_survives_metric_clarification_and_branch_move(repository):
    root, git, gateway = repository

    class ManyMetrics(DemoProfilerGateway):
        def get_list_metrics(self, result_id):
            return [f"m{i}" for i in range(7)]

    executor = ProfileAnalysisExecutor(ProfileAnalysisAgent(ManyMetrics(), source_gateway=gateway))
    task = PerformanceTask(
        summary="Inspect",
        target={"repository": "repo", "branch": "main", "profile": "id"},
        details={
            "profile_analysis": {
                "source": {"locations": [{"path": "hot.c", "start_line": 1, "end_line": 3}]}
            }
        },
    )
    _, cursor = executor.step("run", task, {})
    event, cursor = executor.step("run", task, cursor)
    assert event.type == "progress" and cursor["source_snapshot"]["status"] == "resolved"
    pinned = cursor["source_snapshot"]["commit"]
    (root / "hot.c").write_text("changed\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "move")
    event, cursor = executor.step("run", task, cursor)
    assert event.type == "clarification"
    event, cursor = executor.step(
        "run",
        task,
        cursor,
        ClarificationAnswer(
            request_id=event.clarification.request_id,
            answer='["m1"]',
        ),
    )
    assert event.type == "result" and cursor["source_snapshot"]["commit"] == pinned
    assert "return 1" in cursor["analysis"]["source_context"]["evidence"][0]["text"]


def test_unavailable_snapshot_is_not_re_resolved(repository):
    _, _, gateway = repository
    selection = source()
    agent = ProfileAnalysisAgent(DemoProfilerGateway(), source_gateway=GitSourceGateway())
    snapshot = prepare_source(agent.source_gateway, selection)
    assert snapshot.status == "unavailable"
    agent.source_gateway = gateway
    report = agent.analyze(
        AnalysisRequest(result_id="id", goal="Inspect", source=selection), source_snapshot=snapshot
    )
    assert report.source_context.snapshot.status == "unavailable"
    assert report.source_context.evidence == []


def test_budget_omits_optional_code_and_rejects_omitted_citation(repository):
    root, git, gateway = repository
    (root / "hot.c").write_text("é" * 2000 + "\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "long utf8")
    model = SourceModel(cite_omitted=True)
    agent = ProfileAnalysisAgent(
        DemoProfilerGateway(), model, source_gateway=gateway, max_input_bytes=6500
    )
    report = agent.analyze(AnalysisRequest(result_id="id", goal="Inspect", source=source()))
    assert model.payload is not None and model.payload["source_evidence"] == []
    assert report.status == "insufficient_data" and report.observations == []


def test_explicit_commit_scope_must_match_and_old_reports_validate(repository):
    _, git, gateway = repository
    commit = git("rev-parse", "HEAD").decode().strip()
    selection = source(commit=commit, scope_repository="repo", scope_ref="other")
    assert prepare_source(gateway, selection).status == "unavailable"
    from perf_chat.profiler_analysis_agent import ProfileAnalysis

    report = ProfileAnalysis.model_validate(
        {"status": "completed", "request": {"result_id": "old", "goal": "Inspect"}}
    )
    assert report.source_context is None


def test_required_source_can_be_selected_after_clarification(repository):
    _, _, gateway = repository
    agent = ProfileAnalysisAgent(DemoProfilerGateway(), SourceModel(), source_gateway=gateway)
    executor = ProfileAnalysisExecutor(agent)
    task = PerformanceTask(
        summary="Inspect",
        target={"repository": "repo", "branch": "main", "profile": "id"},
        details={"profile_analysis": {"source": {"policy": "required", "symbols": ["hot"]}}},
    )
    _, cursor = executor.step("run", task, {})
    _, cursor = executor.step("run", task, cursor)
    pinned = cursor["source_snapshot"]["commit"]
    event, cursor = executor.step("run", task, cursor)
    assert event.clarification.fields == ["source"]
    event, cursor = executor.step(
        "run",
        task,
        cursor,
        ClarificationAnswer(
            request_id=event.clarification.request_id,
            answer='{"locations":[{"path":"hot.c","start_line":1,"end_line":3}]}',
        ),
    )
    assert event.type == "result" and cursor["source_snapshot"]["commit"] == pinned
    assert cursor["analysis"]["source_context"]["required_delivery"] == "delivered"


def test_missing_exact_commit_outcome_is_persisted_and_opt_out_works(repository):
    _, _, gateway = repository
    executor = ProfileAnalysisExecutor(
        ProfileAnalysisAgent(DemoProfilerGateway(), SourceModel(), source_gateway=gateway)
    )
    task = PerformanceTask(
        summary="Inspect",
        target={"repository": "repo", "branch": "main", "profile": "id"},
        details={
            "profile_analysis": {
                "source": {
                    "policy": "required",
                    "commit": "0" * 40,
                    "scope_repository": "repo",
                    "scope_ref": "main",
                }
            }
        },
    )
    _, cursor = executor.step("run", task, {})
    _, cursor = executor.step("run", task, cursor)
    assert cursor["source_snapshot"]["status"] == "unavailable"
    event, cursor = executor.step("run", task, cursor)
    assert event.type == "clarification"
    event, cursor = executor.step(
        "run",
        task,
        cursor,
        ClarificationAnswer(
            request_id=event.clarification.request_id,
            answer='{"policy":"off"}',
        ),
    )
    assert event.type == "result"
    assert cursor["source_snapshot"]["status"] == "unavailable"
    assert cursor["options"]["source"] == {"policy": "off"}


def test_worker_restart_retains_prepared_commit(repository, tmp_path):
    root, git, _ = repository
    from perf_chat.config import Settings
    from perf_chat.store import Store
    from perf_chat.worker import Worker

    settings = Settings(
        db_path=str(tmp_path / "run.db"),
        executor_mode="profile-demo",
        git_repositories={"repo": str(root)},
    )
    store = Store(settings.db_path)
    cid = store.create_conversation()["id"]
    task = PerformanceTask(
        summary="Inspect",
        target={"repository": "repo", "branch": "main", "profile": "id"},
        details={
            "profile_analysis": {
                "source": {"locations": [{"path": "hot.c", "start_line": 1, "end_line": 3}]}
            }
        },
    )
    store.update_spec(cid, task, 1)
    rid = store.start_run(cid, 2, "source-run")["id"]
    worker = Worker(settings)
    worker.tick()
    worker.tick()
    snapshot = store.run(rid)["cursor"]["source_snapshot"]
    (root / "hot.c").write_text("new branch version\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "after preparation")
    Worker(settings).tick()
    run = Store(settings.db_path).run(rid)
    assert run["status"] == "demo"
    assert run["cursor"]["source_snapshot"] == snapshot
    assert "return 1" in run["cursor"]["analysis"]["source_context"]["evidence"][0]["text"]


def test_required_source_omission_requests_narrower_range(repository):
    root, git, gateway = repository
    (root / "hot.c").write_text("é" * 2000 + "\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "large source range")
    model = SourceModel()
    report = ProfileAnalysisAgent(
        DemoProfilerGateway(), model, source_gateway=gateway, max_input_bytes=6500
    ).analyze(
        AnalysisRequest(result_id="id", goal="Inspect", source=source(policy="required")),
    )
    assert report.status == "needs_input" and report.clarification_field == "source"
    assert model.payload is None


def test_line_truncation_preserves_complete_lines_and_hash(repository):
    root, git, gateway = repository
    (root / "hot.c").write_text("  first line\n" + "x" * 9000 + "\nlast\n", encoding="utf-8")
    git("add", "hot.c")
    git("commit", "-m", "long line")
    snapshot = prepare_source(gateway, source())
    excerpt = gateway.read_source("repo", snapshot.commit, source().locations[0])
    assert excerpt.text == "  first line\n" and excerpt.end_line == 1
    assert excerpt.sha256 == hashlib.sha256(excerpt.text.encode()).hexdigest()


def test_provider_token_budget_and_mismatched_snapshot(repository):
    builder = PromptContextBuilder(token_counter=lambda body: 100, max_tokens=10)
    with pytest.raises(ContextBudgetExceeded):
        builder.build(object(), "system", {"evidence": [{"id": "e1"}], "documentation": None}, None)
    _, _, gateway = repository
    snapshot = prepare_source(gateway, source())
    changed = source()
    changed.ref = "other"
    with pytest.raises(ValueError, match="snapshot does not match"):
        ProfileAnalysisAgent(DemoProfilerGateway(), source_gateway=gateway).analyze(
            AnalysisRequest(result_id="id", goal="Inspect", source=changed),
            source_snapshot=snapshot,
        )
