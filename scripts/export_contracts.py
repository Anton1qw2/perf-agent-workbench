"""Run from the project root after installing the package."""

import json
from pathlib import Path

from perf_chat.contracts import (
    ClarificationAnswer,
    ClarificationRequest,
    ExecutionEvent,
    PerformanceResult,
    PerformanceTask,
)
from perf_chat.profiler_analysis_agent.context_contracts import (
    AnalysisContext,
    AnalysisContextUpdate,
    ContextUpdateProposal,
)
from perf_chat.profiler_analysis_agent.contracts import (
    AnalysisRequest,
    AnalysisStep,
    ChatReport,
    MCPTool,
    ProfileAnalysis,
)
from perf_chat.profiler_analysis_agent.source_contracts import (
    SourceContext,
    SourceEvidence,
    SourceRequest,
    SourceSnapshot,
)

destination = Path("schemas")
destination.mkdir(exist_ok=True)
for model in [
    PerformanceTask,
    ExecutionEvent,
    ClarificationRequest,
    ClarificationAnswer,
    PerformanceResult,
    AnalysisRequest,
    ProfileAnalysis,
    AnalysisStep,
    AnalysisContext,
    AnalysisContextUpdate,
    ContextUpdateProposal,
    ChatReport,
    MCPTool,
    SourceRequest,
    SourceSnapshot,
    SourceEvidence,
    SourceContext,
]:
    (destination / f"{model.__name__}.json").write_text(
        json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
