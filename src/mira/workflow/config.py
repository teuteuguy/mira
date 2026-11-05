"""Workflow configuration: what Mira runs for a trigger, in which order.

A workflow is an ordered list of stages. Steps inside a stage run in
parallel; stages run one after another. A step is either one of Mira's
built-in review passes or (later) a custom agent. Each step decides what its
findings do: join the output (``append``), join the output and possibly stop
the workflow (``block``), or only be reported (``report``).

With no workflow configured, :func:`default_workflow` reproduces Mira's
historical behaviour: a single stage running every enabled built-in pass.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from mira.config import ReviewConfig

Trigger = Literal["pull_request"]
ResultMode = Literal["append", "block", "report"]
SeverityName = Literal["blocker", "warning", "suggestion", "nitpick"]

# Built-in steps, in the order the default workflow lists them.
BUILTIN_REVIEW = "mira.review"
BUILTIN_SECURITY = "mira.security"
BUILTIN_DEPENDENCIES = "mira.dependencies"
BUILTIN_OSV = "mira.osv"
BUILTIN_SECRETS = "mira.secrets"
BUILTIN_WALKTHROUGH = "mira.walkthrough"
BUILTIN_OVERLAP = "mira.overlap"

BUILTIN_STEPS: tuple[str, ...] = (
    BUILTIN_REVIEW,
    BUILTIN_SECURITY,
    BUILTIN_DEPENDENCIES,
    BUILTIN_OSV,
    BUILTIN_SECRETS,
    BUILTIN_WALKTHROUGH,
    BUILTIN_OVERLAP,
)

# Steps that only feed the walkthrough comment. They produce no findings, so
# they can't block, and they run alongside the whole workflow (the walkthrough
# is posted early, before findings exist) wherever they are listed.
OUTPUT_STEPS: frozenset[str] = frozenset({BUILTIN_WALKTHROUGH, BUILTIN_OVERLAP})


class BlockWhen(BaseModel):
    """Which findings make a ``block`` step blocking."""

    min_severity: SeverityName = "blocker"


class StepConfig(BaseModel):
    """One unit of work in a stage: a built-in pass or an agent."""

    builtin: str | None = None
    agent: str | None = None
    # Default: ``append`` for built-in steps, ``block`` for agents.
    on_result: ResultMode | None = None
    block_when: BlockWhen = Field(default_factory=BlockWhen)
    # What a blocking step does to the rest of the workflow: ``stop`` skips
    # the later stages, ``continue`` runs them and fails at the end.
    on_block: Literal["stop", "continue"] = "stop"
    # What a step that raises does: ``continue`` records the error and moves
    # on (the historical behaviour of every built-in pass), ``block`` treats
    # the error as a blocking result.
    on_error: Literal["continue", "block"] = "continue"
    # Pass this step's summary to the LLM steps of later stages as context.
    share_summary: bool = False

    @model_validator(mode="after")
    def _one_kind(self) -> StepConfig:
        if (self.builtin is None) == (self.agent is None):
            raise ValueError("a step needs exactly one of `builtin` or `agent`")
        if self.builtin is not None and self.builtin not in BUILTIN_STEPS:
            raise ValueError(
                f"unknown built-in step {self.builtin!r}; expected one of: "
                + ", ".join(BUILTIN_STEPS)
            )
        if self.builtin in OUTPUT_STEPS and self.on_result in ("block", "report"):
            raise ValueError(f"{self.builtin} produces no findings and can only `append`")
        return self

    @property
    def name(self) -> str:
        return self.builtin if self.builtin is not None else f"agent:{self.agent}"

    @property
    def result_mode(self) -> ResultMode:
        if self.on_result is not None:
            return self.on_result
        return "append" if self.builtin is not None else "block"


class StageConfig(BaseModel):
    name: str
    steps: list[StepConfig] = Field(min_length=1)


class WorkflowConfig(BaseModel):
    name: str
    trigger: Trigger = "pull_request"
    # ``owner/repo`` glob patterns; empty matches every repository.
    repos: list[str] = Field(default_factory=list)
    enabled: bool = True
    stages: list[StageConfig] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("workflow name must not be empty")
        return v.strip()

    @model_validator(mode="after")
    def _unique_steps(self) -> WorkflowConfig:
        seen: set[str] = set()
        for stage in self.stages:
            for step in stage.steps:
                if step.name in seen:
                    raise ValueError(f"step {step.name!r} appears more than once")
                seen.add(step.name)
        stage_names = [s.name for s in self.stages]
        if len(stage_names) != len(set(stage_names)):
            raise ValueError("stage names must be unique within a workflow")
        return self

    def step_names(self) -> set[str]:
        return {step.name for stage in self.stages for step in stage.steps}

    def matches(self, trigger: str, repo: str | None) -> bool:
        if not self.enabled or self.trigger != trigger:
            return False
        if not self.repos:
            return True
        if repo is None:
            return False
        repo_l = repo.lower()
        return any(fnmatch.fnmatchcase(repo_l, pattern.lower()) for pattern in self.repos)


DEFAULT_WORKFLOW_NAME = "default"


def default_workflow(review: ReviewConfig) -> WorkflowConfig:
    """Today's review as a workflow: one stage holding every enabled pass.

    The ``review.*`` switches keep deciding which built-in passes run. The
    dependency and OSV passes have their own runtime gates (a manifest must
    change), applied by the step itself.
    """
    steps = [StepConfig(builtin=BUILTIN_REVIEW)]
    if review.security_pass:
        steps.append(StepConfig(builtin=BUILTIN_SECURITY))
    if review.dependency_overlap:
        steps.append(StepConfig(builtin=BUILTIN_DEPENDENCIES))
    if review.osv_scan:
        steps.append(StepConfig(builtin=BUILTIN_OSV))
    if review.secrets_scan:
        steps.append(StepConfig(builtin=BUILTIN_SECRETS))
    if review.walkthrough:
        steps.append(StepConfig(builtin=BUILTIN_WALKTHROUGH))
    if review.overlap.enabled:
        steps.append(StepConfig(builtin=BUILTIN_OVERLAP))
    return WorkflowConfig(
        name=DEFAULT_WORKFLOW_NAME,
        stages=[StageConfig(name="review", steps=steps)],
    )


def select_workflow(
    workflows: list[WorkflowConfig],
    review: ReviewConfig,
    *,
    trigger: str = "pull_request",
    repo: str | None = None,
) -> tuple[WorkflowConfig, bool]:
    """Pick the first enabled workflow matching ``trigger`` and ``repo``.

    Returns ``(workflow, is_default)``; falls back to :func:`default_workflow`.
    """
    for wf in workflows:
        if wf.matches(trigger, repo):
            return wf, False
    return default_workflow(review), True
