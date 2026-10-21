"""Consultation contracts; expert guidance is never measured execution evidence."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExpertQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=16000)

    @field_validator("question")
    @classmethod
    def nonempty_question(cls, value):
        if not value.strip():
            raise ValueError("An expert question is required")
        return value


class ExpertSource(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    title: str = Field(min_length=1, max_length=1000)
    url: str | None = Field(default=None, max_length=4096)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or any(c.isspace() for c in value)
                    or any(c in value for c in "<>\\")):
                raise ValueError("Expert sources require HTTP(S) URLs without credentials")
        return value


class ExpertResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    status: Literal["answered", "needs_input", "unavailable", "not_found"]
    answer: str = Field(min_length=1, max_length=16000)
    sources: list[ExpertSource] = Field(default_factory=list, max_length=64)
    limitations: list[Annotated[str, Field(max_length=2000)]] = Field(default_factory=list, max_length=16)
    question: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("answer", "question")
    @classmethod
    def nonempty_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Expert text must not be empty")
        return value

@dataclass(frozen=True)
class ContextPolicy:
    conversation_history: bool = False
    task: bool = False


@dataclass(frozen=True)
class ExpertDefinition:
    id: str
    name: str
    description: str
    input_schema: type[BaseModel] = ExpertQuestion
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)


@dataclass(frozen=True)
class ExpertRequest:
    inputs: BaseModel
    history: tuple[dict, ...] = ()
    task: dict | None = None
    deadline: float | None = None


class Expert(Protocol):
    def ask(self, request: ExpertRequest) -> ExpertResponse: ...


@dataclass(frozen=True)
class ExpertServices:
    """Explicitly supplied backend dependencies; no Store or execution gateways."""

    model: Any = None
