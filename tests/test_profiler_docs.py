from perf_chat.contracts import PerformanceTask
from perf_chat.orchestrator.agent import OrchestratorAgent
from perf_chat.orchestrator.graph import build_intake_graph
from perf_chat.profiler_docs_agent import ProfilerDocsAgent
from perf_chat.task_agent.agent import TaskAgent


class RecordingModel:
    def __init__(self):
        self.calls = []

    def complete(self, system, payload):
        self.calls.append(payload)
        return "Use documented collection [Perf.md:L1-L3]."


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


def test_search_reloads_document_and_returns_sources(tmp_path):
    path = tmp_path / "Perf.md"
    path.write_text("# Collection\nCollect hotspots.\nExport a report.", encoding="utf-8")
    model = RecordingModel()
    agent = ProfilerDocsAgent(model, path)
    result = agent.ask("Perf hotspots", version="2026", platform="Linux")
    assert result.status == "found"
    assert result.sources[0].start_line == 1
    assert result.sources[0].end_line == 3
    assert result.sources[0].file == "Perf.md"
    assert model.calls[0]["version"] == "2026"
    assert model.calls[0]["platform"] == "Linux"
    assert model.calls[0]["sources"][0]["text"] == path.read_text(encoding="utf-8")
    path.write_text("# Other topic\nMemory bandwidth.", encoding="utf-8")
    assert agent.ask("Perf hotspots").status == "not_found"
    assert len(model.calls) == 1


def test_tool_and_demo_excerpts(tmp_path):
    path = tmp_path / "Perf.md"
    path.write_text("# Hotspots\nSample documentation only.", encoding="utf-8")
    result = ProfilerDocsAgent(knowledge_path=path).as_tool().invoke({
        "question": "Perf hotspots", "platform": "Windows",
    })
    assert result["status"] == "found"
    assert "[Perf.md:L1-L2]" in result["answer"]
    assert "compatibility is unverified" in result["answer"]


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
