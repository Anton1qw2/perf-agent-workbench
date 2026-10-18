"""Default analyzer factory; applications may inject their own factory into Worker."""

from .agent import ProfileAnalysisAgent
from .contracts import AnalysisServices, ProfileAnalyzer
from .dynamic import DynamicProfilerAnalyzer


def create_builtin_analyzer(services: AnalysisServices) -> ProfileAnalyzer:
    implementation = ProfileAnalysisAgent if services.profiler.demo else DynamicProfilerAnalyzer
    return implementation(
        services.profiler, services.model, source_gateway=services.source,
        max_input_bytes=services.max_input_bytes,
    )
