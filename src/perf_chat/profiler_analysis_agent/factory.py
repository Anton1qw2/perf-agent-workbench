"""Single built-in analyzer implementation, replaceable through a worker factory."""

from ..profiler_docs_agent import ProfilerDocsAgent
from .contracts import AnalysisServices, ProfileAnalyzer
from .demo import DemoAnalysisModel
from .dynamic import DynamicProfilerAnalyzer


def create_builtin_analyzer(services: AnalysisServices) -> ProfileAnalyzer:
    model = services.model
    docs_agent = None
    if services.profiler.demo and model is None:
        model = DemoAnalysisModel()
        docs_agent = ProfilerDocsAgent()
    return DynamicProfilerAnalyzer(
        services.profiler, model, docs_agent=docs_agent, source_gateway=services.source,
        max_input_bytes=services.max_input_bytes,
        git_gateway=services.git,
    )
