"""Strict public configuration and report schemas."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expectation(Model):
    status: int = Field(ge=100, le=599)
    json_equals: dict[str, Any] = Field(default_factory=dict)
    max_latency_ms: float | None = Field(default=None, gt=0)


class Step(Model):
    name: str = Field(min_length=1, max_length=120)
    operation: str
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = None
    expect: Expectation
    allow_invalid_request: bool = False


class Case(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    replay_safe: bool = False
    setup: list[Step] = Field(default_factory=list, max_length=20)
    steps: list[Step] = Field(min_length=1, max_length=100)
    teardown: list[Step] = Field(default_factory=list, max_length=20)


class Suite(Model):
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    seed: int = 42
    cases: list[Case] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        return self


class StepResult(Model):
    name: str
    phase: Literal["setup", "test", "teardown"]
    status: Literal["passed", "failed", "error"]
    elapsed_ms: float = 0
    http_status: int | None = None
    attempts: int = 1
    messages: list[str] = Field(default_factory=list)


class CaseResult(Model):
    case_id: str
    status: Literal["passed", "failed", "error"]
    elapsed_ms: float = 0
    worker_id: str
    delivery: int = 1
    steps: list[StepResult] = Field(default_factory=list)
    message: str | None = None


class RunReport(Model):
    run_id: str
    suite: str
    mode: Literal["local", "distributed"]
    elapsed_ms: float
    expected: int
    results: list[CaseResult]
    infrastructure_error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.infrastructure_error or len(self.results) != self.expected:
            return 2
        if any(r.status == "error" for r in self.results):
            return 2
        return int(any(r.status == "failed" for r in self.results))
