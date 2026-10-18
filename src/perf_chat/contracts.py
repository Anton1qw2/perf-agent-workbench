"""Versioned integration boundary. Share this module with the executor owner."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Target(StrictModel):
    repository: str | None = None
    branch: str | None = None
    profile: str | None = None  # Opaque reference; profile-analysis execution treats it as result_id.
    # Keep legacy snapshots readable; revision is no longer required or shown in UI.
    revision: str | None = None
    scope: str | None = None


class Objective(StrictModel):
    metric: str | None = None
    direction: Literal["minimize", "maximize"] = "minimize"
    target: float | None = None
    unit: str | None = None


class Baseline(StrictModel):
    value: float | None = None
    evidence: str | None = None
    collect_before_changes: bool = True


class Benchmark(StrictModel):
    scenario: str | None = None
    environment: str | None = None
    concurrency: int | None = Field(default=None, gt=0)
    duration_seconds: int | None = Field(default=None, gt=0)


class Constraints(StrictModel):
    preserve_api: bool = True
    preserve_correctness: bool = True
    notes: list[str] = Field(default_factory=list)


class Budget(StrictModel):
    max_iterations: int = Field(default=3, ge=1, le=100)
    max_runtime_seconds: int = Field(default=600, ge=1, le=86400)


class PerformanceTask(StrictModel):
    schema_version: Literal["1"] = "1"
    task_id: str = ""
    spec_version: int = Field(default=1, ge=1)
    summary: str = ""
    target: Target = Field(default_factory=Target)
    objective: Objective = Field(default_factory=Objective)
    baseline: Baseline = Field(default_factory=Baseline)
    benchmark: Benchmark = Field(default_factory=Benchmark)
    constraints: Constraints = Field(default_factory=Constraints)
    budget: Budget = Field(default_factory=Budget)
    assumptions: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.target.repository and not self.target.profile:
            missing.append("target.repository_or_profile")
        if not self.summary:
            missing.append("summary")
        if self.open_questions:
            missing.append("open_questions")
        return missing


class ClarificationRequest(StrictModel):
    request_id: str
    question: str
    fields: list[str] = Field(default_factory=list)


class ClarificationAnswer(StrictModel):
    request_id: str
    answer: str = Field(min_length=1, max_length=20000)


class Measurement(StrictModel):
    metric: str
    value: float
    unit: str
    evidence: str


class PerformanceResult(StrictModel):
    schema_version: Literal["1"] = "1"
    task_id: str
    status: Literal["succeeded", "failed", "cancelled", "demo"]
    summary: str
    before: list[Measurement] = Field(default_factory=list)
    after: list[Measurement] = Field(default_factory=list)
    correctness_verified: bool = False
    artifacts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExecutionEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    type: Literal["progress", "clarification", "result"]
    message: str = ""
    clarification: ClarificationRequest | None = None
    result: PerformanceResult | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.type == "clarification" and self.clarification is None:
            raise ValueError("clarification payload is required")
        if self.type == "result" and self.result is None:
            raise ValueError("result payload is required")
        return self


class ExecutorGateway(Protocol):
    """One durable step; retries use the same run_id and cursor.

    Executor must deduplicate side effects by run_id/cursor and enforce budget.
    A clarification leaves cursor unchanged until an answer is supplied.
    """

    def step(
        self,
        run_id: str,
        task: PerformanceTask,
        cursor: dict[str, Any],
        answer: ClarificationAnswer | None = None,
    ) -> tuple[ExecutionEvent, dict[str, Any]]: ...

    def cancel(self, run_id: str) -> None: ...
