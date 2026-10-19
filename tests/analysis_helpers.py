"""Helpers driving the single durable analyzer in integration tests."""

import json

from perf_chat.integrations.profiler_mcp import DemoProfilerGateway
from perf_chat.profiler_analysis_agent import DynamicProfilerAnalyzer
from perf_chat.profiler_analysis_agent.demo import DemoAnalysisModel
from perf_chat.profiler_docs_agent import ProfilerDocsAgent


class PlanningModel(DemoAnalysisModel):
    def __init__(self, interpretation=None, questions=None):
        self.interpretation = interpretation
        self.questions = list(questions or [])

    def complete(self, system, payload, **kwargs):
        if payload["schema"]["title"] == "ToolDecision":
            if self.questions:
                return json.dumps({"action": "ask", "reason": "Need context", "question": self.questions.pop(0)})
            return super().complete(system, payload, **kwargs)
        if self.interpretation:
            return self.interpretation.complete(system, payload, **kwargs)
        return super().complete(system, payload, **kwargs)


def analysis_agent(gateway=None, model=None, **kwargs):
    return DynamicProfilerAnalyzer(
        gateway or DemoProfilerGateway(), PlanningModel(model), docs_agent=ProfilerDocsAgent(), **kwargs,
    )


def advance(executor, run_id, task, cursor, answer=None):
    for _ in range(60):
        event, cursor = executor.step(run_id, task, cursor, answer)
        answer = None
        if event.type != "progress":
            return event, cursor
    raise AssertionError("Analyzer did not reach a question or result")


def drain_worker(worker, run_id):
    for _ in range(60):
        run = worker.store.run(run_id)
        if run["status"] in {"waiting", "succeeded", "failed", "cancelled", "demo"}:
            return run
        assert worker.tick()
    raise AssertionError("Worker did not reach a stable state")
