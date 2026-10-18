from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ..contracts import StrictModel


class SourceLocation(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=80, ge=1)

    @model_validator(mode="after")
    def valid_range(self):
        if not 0 <= self.end_line - self.start_line < 80:
            raise ValueError("Select an ordered range of at most 80 lines")
        return self


class SourceOptions(StrictModel):
    policy: Literal["off", "optional", "required"] = "optional"
    commit: str | None = Field(default=None, pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
    scope_repository: str | None = None
    scope_ref: str | None = None
    locations: list[SourceLocation] = Field(default_factory=list, max_length=5)
    symbols: list[str] = Field(default_factory=list, max_length=3)


class SourceRequest(SourceOptions):
    repository: str = Field(min_length=1, max_length=4096)
    ref: str = Field(min_length=1, max_length=256)


class SourceSnapshot(StrictModel):
    status: Literal["resolved", "unavailable", "disabled"]
    repository: str
    requested_ref: str
    requested_commit: str | None = None
    commit: str | None = None
    tree: str | None = None
    reason: str = ""
    preparation_seconds: float = Field(default=0, ge=0)
    selection_basis: Literal["branch_tip", "user_declared"] = "branch_tip"
    profile_revision_match: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def resolved_identity(self):
        if self.status == "resolved" and (not self.commit or not self.tree):
            raise ValueError("Resolved source requires commit and tree identities")
        return self


class SourceEvidence(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    id: str
    repository: str
    commit: str
    blob: str
    path: str
    start_line: int
    end_line: int
    text: str
    sha256: str
    resolution: Literal["provided_location", "text_candidate"]
    origin: Literal["user_selection"] = "user_selection"


class SourceContext(StrictModel):
    snapshot: SourceSnapshot | None = None
    evidence: list[SourceEvidence] = Field(default_factory=list)
    candidates: list[SourceLocation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    needs_selection: bool = False
    required_delivery: Literal[
        "not_required", "pending", "delivered", "interpretation_disabled"
    ] = "not_required"
