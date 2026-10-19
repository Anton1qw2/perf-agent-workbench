import json

from analysis_helpers import advance, analysis_agent
from markdown_it import MarkdownIt

from perf_chat.contracts import PerformanceTask
from perf_chat.profiler_analysis_agent import AnalysisRequest
from perf_chat.profiler_analysis_agent.contracts import Observation, ProfileAnalysis
from perf_chat.profiler_analysis_agent.executor import ProfileAnalysisExecutor
from perf_chat.profiler_analysis_agent.reporting import format_chat_report


class InterpretationModel:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def complete(self, system, payload, **kwargs):
        self.calls += 1
        return self.output


def test_json_fence_is_normalized_before_validating_findings():
    output = json.dumps({"observations": [{"text": "The server reported 120 samples.",
                                            "evidence_ids": ["e1"]}]})
    model = InterpretationModel("```json\n" + output + "\n```")
    report = analysis_agent(model=model).analyze(AnalysisRequest(result_id="id", goal="Inspect"))
    assert report.status == "completed"
    assert report.chat_report.format == "markdown"
    assert "### Observations" in report.chat_report.content
    assert "120 samples" in report.chat_report.content
    assert model.calls == 1


def test_freeform_output_reaches_durable_formatter_without_becoming_findings():
    output = "<h1>Possible issue</h1><p>Inspect counters.</p><script>bad()</script>"
    model = InterpretationModel(output)
    agent = analysis_agent(model=model)
    request = AnalysisRequest(result_id="id", goal="Inspect")
    checkpoint = {}
    for _ in range(20):
        step = agent.step_analysis(request, checkpoint=checkpoint)
        checkpoint = step.checkpoint
        if checkpoint.get("phase") == "format_report":
            assert step.report is None
            break
    else:
        raise AssertionError("Missing formatter node")
    assert checkpoint["raw_interpretation"] == output
    # Reconstruct the analyzer: formatting must not repeat interpretation/model calls.
    fresh = analysis_agent(model=model)
    step = fresh.step_analysis(request, checkpoint=checkpoint)
    assert step.report.status == "insufficient_data"
    assert step.report.observations == []
    content = step.report.chat_report.content
    assert "Unvalidated analyzer response" in content
    assert "Possible issue" in content and "Inspect counters" in content
    assert "<h1>" not in content and "bad()" not in content
    assert model.calls == 1
    assert fresh.step_analysis(request, checkpoint=step.checkpoint).report.chat_report.content == content
    assert model.calls == 1


def test_invalid_citations_remain_unvalidated_after_formatting():
    output = json.dumps({"observations": [{"text": "Unproven claim", "evidence_ids": ["missing"]}]})
    report = analysis_agent(model=InterpretationModel(output)).analyze(
        AnalysisRequest(result_id="id", goal="Inspect"),
    )
    assert report.status == "insufficient_data"
    assert report.observations == []
    assert "not a validated finding" in report.chat_report.content
    assert report.evidence


def test_large_report_uses_bounded_safe_markdown_and_keeps_original_data():
    report = ProfileAnalysis(
        status="completed", request=AnalysisRequest(result_id="id", goal="Inspect"),
        observations=[Observation(text="<b>Important</b> " + "x" * 30000,
                                  evidence_ids=["e1"]) for _ in range(20)],
        limitations=["Unknown units"], documentation={"sources": [
            {"title": "Manual", "url": "https://docs.example.com/cpu"},
            {"title": "Unsafe", "url": "javascript:alert(1)"},
        ]},
    )
    before = report.model_dump()
    content = format_chat_report(report).content
    assert len(content) <= 16000
    assert "Chat display shortened" in content
    assert "<b>" not in content and "javascript:" not in content
    assert report.model_dump() == before
    tokens = MarkdownIt().parse(content)
    assert any(token.type == "heading_open" for token in tokens)


def test_executor_formats_custom_structured_reports_using_the_same_contract():
    class CustomAnalyzer:
        analyzer_id = "custom-report"

        def step_analysis(self, request, **kwargs):
            return {"checkpoint": {}, "report": ProfileAnalysis(
                status="completed", request=request,
                recommendations=["<div>Inspect the counters.</div>"],
                chat_report={"content": "<div>Unsupported custom presentation</div>"},
            ).model_dump()}

    event, cursor = advance(ProfileAnalysisExecutor(CustomAnalyzer()), "run",
                            PerformanceTask(summary="Inspect", target={"profile": "id"}), {})
    assert event.type == "result"
    assert cursor["analysis"]["chat_report"]["format"] == "markdown"
    assert event.result.summary == cursor["analysis"]["chat_report"]["content"]
    assert "<div>" not in event.result.summary
    assert "Recommended checks" in event.result.summary
