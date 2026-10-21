"""Default experts use the same registration boundary as application extensions."""

from ..profiler_docs_agent import ProfilerDocsAgent
from ..profiler_docs_agent.agent import DocumentationQuestion
from ..profiler_docs_agent.trigger import matches_documentation_trigger
from .contracts import ExpertDefinition
from .registry import ExpertRegistry


def default_experts(docs_agent: ProfilerDocsAgent | None = None):
    registry = ExpertRegistry()
    registry.register(ExpertDefinition(
        id="profiler_docs", name="Profiler documentation",
        description="Answers questions using the complete local profiler documentation.",
        input_schema=DocumentationQuestion,
    ), factory=lambda services: (
        docs_agent if docs_agent is not None else ProfilerDocsAgent(services.model)
    ), matcher=matches_documentation_trigger)
    return registry
