"""Run a workflow's stages and record what each step did.

The runner knows nothing about reviews: the engine hands it one coroutine
factory per step and a function that decides which findings block. That
keeps the stage logic (ordering, blocking, error handling, sharing
summaries) testable on its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from mira.models import KeyIssue, ReviewComment
from mira.workflow.config import OUTPUT_STEPS, StepConfig, WorkflowConfig

logger = logging.getLogger(__name__)

StepStatus = Literal["passed", "blocked", "error", "skipped"]


@dataclass
class StepOutput:
    """What a step produced."""

    comments: list[ReviewComment] = field(default_factory=list)
    key_issues: list[KeyIssue] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    # Set when the step had nothing to do (e.g. no manifest changed).
    skipped_reason: str | None = None


@dataclass
class StepContext:
    """What a step can see from earlier stages."""

    shared_summaries: list[str] = field(default_factory=list)

    def shared_context(self) -> str:
        """Markdown block for LLM prompts; empty when nothing was shared."""
        if not self.shared_summaries:
            return ""
        return "## Findings from earlier review steps\n\n" + "\n\n".join(self.shared_summaries)


StepFn = Callable[[StepContext], Awaitable[StepOutput]]
# Returns (comments to keep for this step, the subset that blocks).
BlockCheck = Callable[
    [StepConfig, list[ReviewComment]], Awaitable[tuple[list[ReviewComment], list[ReviewComment]]]
]


@dataclass
class StepRun:
    stage: str
    step: StepConfig
    status: StepStatus = "skipped"
    duration_s: float = 0.0
    output: StepOutput = field(default_factory=StepOutput)
    blocking: list[ReviewComment] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def name(self) -> str:
        return self.step.name


@dataclass
class StageRun:
    name: str
    steps: list[StepRun] = field(default_factory=list)
    # Set when an earlier stage blocked with ``on_block: stop``.
    skipped: bool = False


@dataclass
class WorkflowRun:
    workflow: str
    is_default: bool
    stages: list[StageRun] = field(default_factory=list)

    @property
    def steps(self) -> list[StepRun]:
        return [s for stage in self.stages for s in stage.steps]

    @property
    def blocked_by(self) -> list[StepRun]:
        return [s for s in self.steps if s.status == "blocked"]

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

    @property
    def skipped_stages(self) -> list[str]:
        return [s.name for s in self.stages if s.skipped]

    def outputs(self, mode: str) -> list[StepRun]:
        """Step runs whose findings use the given result mode and produced output."""
        return [
            s
            for s in self.steps
            if s.status in ("passed", "blocked") and s.step.result_mode == mode
        ]


def _summary_for_sharing(run: StepRun) -> str:
    lines = [f"### {run.name}"]
    lines.extend(s for s in run.output.summaries if s)
    for c in run.output.comments[:10]:
        lines.append(f"- {c.severity.name.lower()}: {c.title} (`{c.path}:{c.line}`)")
    return "\n".join(lines) if len(lines) > 1 else ""


async def run_workflow(
    workflow: WorkflowConfig,
    *,
    is_default: bool,
    steps: dict[str, StepFn],
    block_check: BlockCheck,
) -> WorkflowRun:
    """Run ``workflow`` stage by stage.

    ``steps`` maps step names to implementations. Output-only steps
    (walkthrough, overlap) are run by the caller and skipped here. A step
    with no implementation (e.g. an agent that isn't configured) is recorded
    as skipped.
    """
    run = WorkflowRun(workflow=workflow.name, is_default=is_default)
    shared: list[str] = []
    stop = False

    for stage in workflow.stages:
        stage_run = StageRun(name=stage.name)
        run.stages.append(stage_run)
        if stop:
            stage_run.skipped = True
            stage_run.steps = [
                StepRun(stage=stage.name, step=s, skipped_reason="earlier stage blocked")
                for s in stage.steps
            ]
            continue

        ctx = StepContext(shared_summaries=list(shared))

        async def _run_step(
            step: StepConfig, _stage: str = stage.name, ctx: StepContext = ctx
        ) -> StepRun:
            sr = StepRun(stage=_stage, step=step)
            if step.builtin in OUTPUT_STEPS:
                sr.status = "passed"
                return sr
            impl = steps.get(step.name)
            if impl is None:
                sr.skipped_reason = "not available"
                logger.warning("Workflow %s: step %s is not available", workflow.name, step.name)
                return sr
            started = time.monotonic()
            try:
                sr.output = await impl(ctx)
            except Exception as exc:
                sr.duration_s = time.monotonic() - started
                sr.error = f"{type(exc).__name__}: {exc}"
                logger.warning("Workflow %s: step %s failed: %s", workflow.name, step.name, exc)
                sr.status = "blocked" if step.on_error == "block" else "error"
                return sr
            sr.duration_s = time.monotonic() - started
            if sr.output.skipped_reason:
                sr.skipped_reason = sr.output.skipped_reason
                sr.status = "skipped"
                return sr
            if step.result_mode == "block" and sr.output.comments:
                sr.output.comments, sr.blocking = await block_check(step, sr.output.comments)
            sr.status = "blocked" if sr.blocking else "passed"
            return sr

        stage_run.steps = list(await asyncio.gather(*[_run_step(s) for s in stage.steps]))

        for sr in stage_run.steps:
            if sr.step.share_summary and sr.status in ("passed", "blocked"):
                text = _summary_for_sharing(sr)
                if text:
                    shared.append(text)
            if sr.status == "blocked" and sr.step.on_block == "stop":
                stop = True

    return run
