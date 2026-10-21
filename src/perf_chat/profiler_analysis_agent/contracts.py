from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field

from ..contracts import StrictModel
from .context_contracts import AnalysisContext, AnalysisContextUpdate, ContextUpdateProposal
from .source_contracts import SourceContext, SourceRequest


class AnalysisRequest(StrictModel):
    context_revision: int = Field(default=0, ge=0, le=8)
    context_feedback: str = Field(default="", max_length=2000)
    result_id: str = Field(default="", max_length=4096)
    goal: str = Field(min_length=1, max_length=20000)
    repository: str | None = Field(default=None, max_length=4096)
    branch: str | None = Field(default=None, max_length=256)
    metrics: list[str] = Field(default_factory=list, max_length=6)
    include_asm: bool = False
    source: SourceRequest | None = None


class Evidence(StrictModel):
    id: str
    context: AnalysisContext | None = None
    server: Literal["profiler", "git"] = "profiler"
    operation: str = Field(min_length=1, max_length=256)
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


class ChatReport(StrictModel):
    format: Literal["markdown"] = "markdown"
    content: str = Field(min_length=1, max_length=16000)


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
    clarification_field: Literal["metrics", "source", "analysis_input"] = "metrics"
    tool_catalog: list[dict[str, Any]] = Field(default_factory=list)
    git_tool_catalog: list[dict[str, Any]] = Field(default_factory=list)
    analyzer: str = "legacy"
    chat_report: ChatReport | None = None
    context_updates: list[AnalysisContextUpdate] = Field(default_factory=list, max_length=8)
    source_history: list[SourceContext] = Field(default_factory=list, max_length=8)



class MCPTool(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=16000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class MCPToolGateway(Protocol):
    allowed_tools: frozenset[str]
    configuration_fingerprint: str

    def list_tools(self) -> list[MCPTool]: ...
    def call_tool(self, tool: MCPTool, arguments: dict, *, result_id: str) -> Any: ...


class AnalysisStep(StrictModel):
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    message: str = "Analysis step completed."
    report: ProfileAnalysis | None = None
    context_update: ContextUpdateProposal | None = None


class ProfileAnalyzer(Protocol):
    """Replaceable analysis lifecycle. Checkpoints must be JSON-serializable.

    Called again after each durable progress event or clarification. Read-only
    calls may be redelivered before the checkpoint transaction is accepted.
    """

    def step_analysis(
        self, request: AnalysisRequest, *, checkpoint: dict[str, Any],
        answer: str | None = None, deadline: float | None = None, source_snapshot=None,
        context_updates: list[AnalysisContextUpdate] | None = None,
    ) -> AnalysisStep: ...


@dataclass(frozen=True)
class AnalysisServices:
    """Backend services provided to a trusted analyzer factory; no token values."""

    profiler: MCPToolGateway
    analysis_id: str = ""
    model: Any = None
    source: Any = None
    max_input_bytes: int = 120_000
    git: MCPToolGateway | None = None
    demo: bool = False  # Host configuration, never inferred from MCP data or gateways.
