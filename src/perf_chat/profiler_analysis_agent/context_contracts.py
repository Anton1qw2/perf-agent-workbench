"""Host-validated metadata updates, independent of any analyzer graph or MCP tool."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..contracts import StrictModel


class AnalysisContext(StrictModel):
    revision: int = Field(default=0, ge=0, le=8)
    result_id: str = Field(default="", max_length=4096)
    repository: str | None = Field(default=None, max_length=4096)
    branch: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_repository(self):
        if self.repository and "://" in self.repository:
            url = urlsplit(self.repository)
            if ((url.scheme in {"http", "https"} and url.username)
                    or url.password or url.query or url.fragment or "\\" in self.repository):
                raise ValueError("Repository references must not contain credentials or URL parameters")
        return self


class ContextUpdateProposal(StrictModel):
    expected_revision: int = Field(ge=0, le=7)
    values: dict[Literal["result_id", "repository", "branch"], str | None] = Field(min_length=1, max_length=3)
    mode: Literal["replace", "compare"] = "replace"

    def apply(self, previous: AnalysisContext) -> AnalysisContext:
        if previous.revision != self.expected_revision:
            raise ValueError("Context revision changed")
        if "result_id" in self.values and self.values["result_id"] is None:
            raise ValueError("Use an empty string to clear Result ID, not null")
        if any(self.values.get(key) == "" for key in ("repository", "branch")):
            raise ValueError("Use null to clear repository/branch, not an empty string")
        values = {**previous.model_dump(), **self.values, "revision": previous.revision + 1}
        if "repository" in self.values and self.values["repository"] != previous.repository:
            values["branch"] = self.values.get("branch")
        current = AnalysisContext.model_validate(values)
        if not current.result_id and not current.repository:
            raise ValueError("A profile or repository is required")
        if current.model_dump(exclude={"revision"}) == previous.model_dump(exclude={"revision"}):
            raise ValueError("The proposed context is unchanged")
        if self.mode == "compare" and (
            not previous.result_id or not current.result_id
            or current.result_id == previous.result_id
            or (current.repository, current.branch) != (previous.repository, previous.branch)
        ):
            raise ValueError("Comparison requires two profiles in the same repository/ref scope")
        return current


class AnalysisContextUpdate(StrictModel):
    update_id: str = Field(min_length=1, max_length=200)
    reply_to: str = Field(min_length=1, max_length=200)
    previous: AnalysisContext
    current: AnalysisContext
    mode: Literal["replace", "compare"] = "replace"

    @model_validator(mode="after")
    def valid_transition(self):
        proposed = ContextUpdateProposal(
            expected_revision=self.previous.revision,
            values=self.current.model_dump(exclude={"revision"}), mode=self.mode,
        ).apply(self.previous)
        if proposed != self.current:
            raise ValueError("Invalid context transition")
        return self
