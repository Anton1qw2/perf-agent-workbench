import json

from perf_chat.contracts import PerformanceTask
from perf_chat.orchestrator.agent import OrchestratorAgent
from perf_chat.orchestrator.graph import build_intake_graph
from perf_chat.profiler_docs_agent import ProfilerDocsAgent
from perf_chat.task_agent.agent import TaskAgent


class RecordingModel:
    def __init__(self):
        self.calls = []

    def complete(self, system, payload, *, json_output=False):
        assert json_output
        self.calls.append(payload)
        return json.dumps({"answer": "Use documented collection.", "supported": True,
                           "source_ids": ["doc1"]})


def test_empty_and_missing_knowledge_never_call_model(tmp_path):
    path = tmp_path / "Perf.md"
    model = RecordingModel()
    agent = ProfilerDocsAgent(model, path)
    assert agent.ask("Perf hotspots").status == "missing"
    path.write_text("", encoding="utf-8")
    result = agent.ask("Perf hotspots")
    assert result.status == "empty"
    assert result.sources == []
    assert "empty" in result.answer
    assert model.calls == []


def test_full_document_reloads_and_returns_only_cited_source_metadata(tmp_path):
    path = tmp_path / "Perf.md"
    path.write_text("# Collection\nCollect hotspots.\nExport a report.", encoding="utf-8")
    model = RecordingModel()
    agent = ProfilerDocsAgent(model, path)
    result = agent.ask("Perf hotspots", version="2026", platform="Linux")
    assert result.status == "found"
    assert result.sources[0].title == "Collection"
    assert "local documentation" in result.answer
    assert result.sources[0].file == "Perf.md"
    assert model.calls[0]["version"] == "2026"
    assert model.calls[0]["platform"] == "Linux"
    assert model.calls[0]["document"] == path.read_bytes().decode("utf-8")
    assert "text" not in result.sources[0].model_dump()
    path.write_text("# Other topic\nMemory bandwidth.", encoding="utf-8")
    assert agent.ask("Perf hotspots").status == "found"
    assert model.calls[1]["document"] == path.read_bytes().decode("utf-8")
    assert len(model.calls) == 2


def test_tool_demo_reports_no_model_synthesis(tmp_path):
    path = tmp_path / "Perf.md"
    path.write_text("# Hotspots\nSample documentation only.", encoding="utf-8")
    result = ProfilerDocsAgent(knowledge_path=path).as_tool().invoke({
        "question": "Perf hotspots", "platform": "Windows",
    })
    assert result["status"] == "found"
    assert "Demo mode cannot answer" in result["answer"]
    assert result["sources"] == []
    assert "Sample documentation only" not in result["answer"]


def test_graph_routes_only_current_perf_request(tmp_path):
    path = tmp_path / "Perf.md"
    path.write_text("", encoding="utf-8")
    graph = build_intake_graph(
        TaskAgent(), OrchestratorAgent(), ProfilerDocsAgent(knowledge_path=path),
    )
    state = {"task": PerformanceTask().model_dump(), "messages": [
        {"role": "user", "content": "How do I collect a Perf profile?"},
    ]}
    result = graph.invoke(state)
    assert result["documentation"]["status"] == "empty"
    assert "knowledge base (Perf.md) is empty" in result["reply"]
    result = graph.invoke({
        "task": result["task"], "messages": state["messages"] + [
            {"role": "user", "content": "Optimize file loading"},
        ],
    })
    assert "documentation" not in result
    assert "knowledge base" not in result["reply"]


def test_packaged_placeholder_is_empty():
    assert ProfilerDocsAgent().knowledge_path.read_bytes() == b""


def test_trigger_file_reload_controls_existing_graph(tmp_path, monkeypatch):
    from perf_chat.profiler_docs_agent import trigger

    keywords = tmp_path / "triggers.txt"
    monkeypatch.setattr(trigger, "TRIGGER_PATH", keywords)
    graph = build_intake_graph(TaskAgent(), OrchestratorAgent())

    def lookup(message):
        return graph.invoke({
            "task": PerformanceTask().model_dump(),
            "messages": [{"role": "user", "content": message}],
        })

    keywords.write_text("# Keywords\n\nvtune\nmy profiler\nc++\n", encoding="utf-8-sig")
    assert "documentation" in lookup("Explain VTune hotspots")
    assert "documentation" in lookup("Use My Profiler, please")
    assert "documentation" in lookup("Explain c++ hotspots")
    assert "documentation" not in lookup("Explain Perf hotspots")
    assert "documentation" not in lookup("Explain vtunelike tools")
    assert "documentation" not in lookup("Explain cxx hotspots")
    keywords.write_text("perf\n", encoding="utf-8")
    assert "documentation" in lookup("Explain Perf hotspots")
    assert "documentation" not in lookup("Explain VTune hotspots")
    keywords.write_text("# Disabled\n", encoding="utf-8")
    assert "documentation" not in lookup("Explain Perf hotspots")
    keywords.write_bytes(b"\xff")
    assert "documentation" not in lookup("Explain Perf hotspots")
    keywords.unlink()
    assert "documentation" not in lookup("Explain Perf hotspots")
