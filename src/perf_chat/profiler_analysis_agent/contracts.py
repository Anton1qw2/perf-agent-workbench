from typing import Any, Literal, Protocol

from pydantic import Field

from ..contracts import StrictModel
from .source_contracts import SourceContext, SourceRequest


class AnalysisRequest(StrictModel):
    result_id: str = Field(min_length=1, max_length=4096)
    goal: str = Field(min_length=1, max_length=20000)
    metrics: list[str] = Field(default_factory=list, max_length=6)
    include_asm: bool = False
    source: SourceRequest | None = None


class Evidence(StrictModel):
    id: str
    operation: Literal["get_list_metrics", "get_hotspots", "get_asm"]
    arguments: dict[str, Any]
    response: Any
    retrieved_at: str
    sha256: str


class Observation(StrictModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1)


class Interpretation(StrictModel):
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Observation] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class ProfileAnalysis(StrictModel):
    schema_version: Literal["1"] = "1"
    status: Literal["completed", "needs_input", "insufficient_data"]
    request: AnalysisRequest
    demo: bool = False
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Observation] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    documentation: dict[str, Any] | None = None
    source_context: SourceContext | None = None
    delivered_evidence_ids: list[str] = Field(default_factory=list)
    clarification_field: Literal["metrics", "source"] = "metrics"


class ProfilerGateway(Protocol):
    demo: bool

    def get_list_metrics(self, result_id: str) -> Any: ...
    def get_hotspots(self, result_id: str, metrics: list[str]) -> Any: ...
    def get_asm(self, result_id: str, metrics: list[str]) -> Any: ...
