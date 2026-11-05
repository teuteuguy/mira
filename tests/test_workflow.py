"""Tests for configurable review workflows (mira.workflow) and their engine wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mira.config import MiraConfig, _strip_deployment_only_settings
from mira.models import ReviewComment, Severity
from mira.workflow import (
    StepConfig,
    StepContext,
    StepOutput,
    WorkflowConfig,
    default_workflow,
    run_workflow,
    select_workflow,
)
from mira.workflow.render import render_workflow_banner, render_workflow_section


def _comment(title: str = "t", severity: Severity = Severity.WARNING, path: str = "a.py"):
    return ReviewComment(
        path=path,
        line=1,
        end_line=None,
        severity=severity,
        category="bug",
        title=title,
        body="b",
        confidence=0.9,
    )


def _wf(stages: list[dict], **kw) -> WorkflowConfig:
    return WorkflowConfig.model_validate({"name": "wf", "stages": stages, **kw})


async def _no_block(step, comments):
    return comments, []


async def _block_blockers(step, comments):
    return comments, [c for c in comments if c.severity >= Severity.BLOCKER]


# ── Config ────────────────────────────────────────────────────────────────


class TestStepConfig:
    def test_needs_exactly_one_kind(self):
        with pytest.raises(ValidationError):
            StepConfig()
        with pytest.raises(ValidationError):
            StepConfig(builtin="mira.review", agent="x")

    def test_unknown_builtin_rejected(self):
        with pytest.raises(ValidationError, match="unknown built-in step"):
            StepConfig(builtin="mira.nope")

    def test_output_steps_cannot_block(self):
        with pytest.raises(ValidationError, match="can only `append`"):
            StepConfig(builtin="mira.walkthrough", on_result="block")

    def test_default_result_mode(self):
        assert StepConfig(builtin="mira.review").result_mode == "append"
        assert StepConfig(agent="sec").result_mode == "block"
        assert StepConfig(agent="sec", on_result="report").result_mode == "report"
        assert StepConfig(agent="sec").name == "agent:sec"


class TestWorkflowConfig:
    def test_duplicate_step_rejected(self):
        with pytest.raises(ValidationError, match="more than once"):
            _wf(
                [
                    {"name": "a", "steps": [{"builtin": "mira.review"}]},
                    {"name": "b", "steps": [{"builtin": "mira.review"}]},
                ]
            )

    def test_duplicate_stage_name_rejected(self):
        with pytest.raises(ValidationError, match="stage names"):
            _wf(
                [
                    {"name": "a", "steps": [{"builtin": "mira.review"}]},
                    {"name": "a", "steps": [{"builtin": "mira.security"}]},
                ]
            )

    def test_repo_matching(self):
        wf = _wf([{"name": "a", "steps": [{"builtin": "mira.review"}]}], repos=["Acme/*"])
        assert wf.matches("pull_request", "acme/api")
        assert not wf.matches("pull_request", "other/api")
        assert not wf.matches("pull_request", None)
        assert _wf([{"name": "a", "steps": [{"builtin": "mira.review"}]}]).matches(
            "pull_request", None
        )

    def test_disabled_workflow_never_matches(self):
        wf = _wf([{"name": "a", "steps": [{"builtin": "mira.review"}]}], enabled=False)
        assert not wf.matches("pull_request", "acme/api")

    def test_unique_workflow_names_in_config(self):
        stages = [{"name": "a", "steps": [{"builtin": "mira.review"}]}]
        with pytest.raises(ValidationError, match="workflow names"):
            MiraConfig.model_validate(
                {"workflows": [{"name": "x", "stages": stages}, {"name": "x", "stages": stages}]}
            )

    def test_workflows_are_deployment_only(self):
        cleaned = _strip_deployment_only_settings(
            {"workflows": [{"name": "x"}], "review": {"walkthrough": False}}
        )
        assert "workflows" not in cleaned
        assert cleaned["review"] == {"walkthrough": False}


class TestSelection:
    def test_default_workflow_follows_review_switches(self):
        cfg = MiraConfig()
        names = default_workflow(cfg.review).step_names()
        assert "mira.review" in names and "mira.security" in names

        cfg.review.security_pass = False
        cfg.review.walkthrough = False
        names = default_workflow(cfg.review).step_names()
        assert "mira.security" not in names
        assert "mira.walkthrough" not in names

    def test_first_match_wins_else_default(self):
        cfg = MiraConfig()
        a = _wf([{"name": "s", "steps": [{"builtin": "mira.review"}]}], repos=["acme/*"])
        b = a.model_copy(update={"name": "b", "repos": []})
        wf, is_default = select_workflow([a, b], cfg.review, repo="acme/api")
        assert (wf.name, is_default) == ("wf", False)
        wf, is_default = select_workflow([a, b], cfg.review, repo="other/api")
        assert (wf.name, is_default) == ("b", False)
        wf, is_default = select_workflow([a], cfg.review, repo="other/api")
        assert is_default and wf.name == "default"


# ── Runner ────────────────────────────────────────────────────────────────


class TestRunner:
    @pytest.mark.asyncio
    async def test_stages_run_in_order_and_steps_in_parallel(self):
        events: list[str] = []
        gate = asyncio.Event()

        async def first(ctx):
            events.append("first")
            gate.set()
            return StepOutput()

        async def parallel_a(ctx):
            events.append("a-start")
            await asyncio.sleep(0)
            events.append("a-end")
            return StepOutput()

        async def parallel_b(ctx):
            events.append("b-start")
            await asyncio.sleep(0)
            events.append("b-end")
            return StepOutput()

        wf = _wf(
            [
                {"name": "one", "steps": [{"builtin": "mira.secrets"}]},
                {"name": "two", "steps": [{"builtin": "mira.review"}, {"builtin": "mira.osv"}]},
            ]
        )
        run = await run_workflow(
            wf,
            is_default=False,
            steps={"mira.secrets": first, "mira.review": parallel_a, "mira.osv": parallel_b},
            block_check=_no_block,
        )
        assert events[0] == "first"
        assert events.index("b-start") < events.index("a-end")
        assert [s.status for s in run.steps] == ["passed", "passed", "passed"]

    @pytest.mark.asyncio
    async def test_block_stop_skips_later_stages(self):
        later = AsyncMock(return_value=StepOutput())

        async def gate(ctx):
            return StepOutput(comments=[_comment("bad", Severity.BLOCKER), _comment("meh")])

        wf = _wf(
            [
                {"name": "gate", "steps": [{"builtin": "mira.security", "on_result": "block"}]},
                {"name": "review", "steps": [{"builtin": "mira.review"}]},
            ]
        )
        run = await run_workflow(
            wf,
            is_default=False,
            steps={"mira.security": gate, "mira.review": later},
            block_check=_block_blockers,
        )
        later.assert_not_called()
        assert run.blocked
        assert [s.name for s in run.blocked_by] == ["mira.security"]
        assert run.skipped_stages == ["review"]
        assert [c.title for c in run.blocked_by[0].blocking] == ["bad"]
        # Both findings still go to the output, not just the blocking one.
        assert len(run.blocked_by[0].output.comments) == 2

    @pytest.mark.asyncio
    async def test_block_continue_runs_everything(self):
        later = AsyncMock(return_value=StepOutput())

        async def gate(ctx):
            return StepOutput(comments=[_comment("bad", Severity.BLOCKER)])

        wf = _wf(
            [
                {
                    "name": "gate",
                    "steps": [
                        {"builtin": "mira.security", "on_result": "block", "on_block": "continue"}
                    ],
                },
                {"name": "review", "steps": [{"builtin": "mira.review"}]},
            ]
        )
        run = await run_workflow(
            wf,
            is_default=False,
            steps={"mira.security": gate, "mira.review": later},
            block_check=_block_blockers,
        )
        later.assert_awaited_once()
        assert run.blocked and run.skipped_stages == []

    @pytest.mark.asyncio
    async def test_append_step_never_blocks(self):
        check = AsyncMock(side_effect=_block_blockers)

        async def step(ctx):
            return StepOutput(comments=[_comment("bad", Severity.BLOCKER)])

        wf = _wf([{"name": "s", "steps": [{"builtin": "mira.review"}]}])
        run = await run_workflow(
            wf, is_default=False, steps={"mira.review": step}, block_check=check
        )
        check.assert_not_called()
        assert not run.blocked

    @pytest.mark.asyncio
    async def test_on_error(self):
        async def boom(ctx):
            raise RuntimeError("nope")

        cont = _wf([{"name": "s", "steps": [{"builtin": "mira.security"}]}])
        run = await run_workflow(
            cont, is_default=False, steps={"mira.security": boom}, block_check=_no_block
        )
        assert run.steps[0].status == "error" and not run.blocked
        assert "nope" in (run.steps[0].error or "")

        blk = _wf([{"name": "s", "steps": [{"builtin": "mira.security", "on_error": "block"}]}])
        run = await run_workflow(
            blk, is_default=False, steps={"mira.security": boom}, block_check=_no_block
        )
        assert run.blocked

    @pytest.mark.asyncio
    async def test_skipped_and_unavailable_steps(self):
        async def nothing(ctx):
            return StepOutput(skipped_reason="no manifest")

        wf = _wf(
            [{"name": "s", "steps": [{"builtin": "mira.osv"}, {"agent": "missing"}]}],
        )
        run = await run_workflow(
            wf, is_default=False, steps={"mira.osv": nothing}, block_check=_no_block
        )
        assert [(s.status, s.skipped_reason) for s in run.steps] == [
            ("skipped", "no manifest"),
            ("skipped", "not available"),
        ]

    @pytest.mark.asyncio
    async def test_share_summary_reaches_later_stages_only(self):
        seen: dict[str, str] = {}

        async def early(ctx):
            seen["early"] = ctx.shared_context()
            return StepOutput(summaries=["threat model: tenants"], comments=[_comment("x")])

        async def late(ctx):
            seen["late"] = ctx.shared_context()
            return StepOutput()

        wf = _wf(
            [
                {"name": "a", "steps": [{"builtin": "mira.security", "share_summary": True}]},
                {"name": "b", "steps": [{"builtin": "mira.review"}]},
            ]
        )
        await run_workflow(
            wf,
            is_default=False,
            steps={"mira.security": early, "mira.review": late},
            block_check=_no_block,
        )
        assert seen["early"] == ""
        assert "threat model: tenants" in seen["late"]
        assert "mira.security" in seen["late"]

    def test_shared_context_empty(self):
        assert StepContext().shared_context() == ""


# ── Rendering ─────────────────────────────────────────────────────────────


class TestRender:
    @pytest.mark.asyncio
    async def test_default_workflow_renders_nothing(self):
        cfg = MiraConfig()
        run = await run_workflow(
            default_workflow(cfg.review), is_default=True, steps={}, block_check=_no_block
        )
        assert render_workflow_section(run) is None
        assert render_workflow_banner(run) is None

    @pytest.mark.asyncio
    async def test_blocked_run(self):
        async def gate(ctx):
            return StepOutput(comments=[_comment("Cross-tenant read", Severity.BLOCKER)])

        async def report(ctx):
            return StepOutput(comments=[_comment("Noted", Severity.SUGGESTION)])

        wf = _wf(
            [
                {
                    "name": "gate",
                    "steps": [
                        {"builtin": "mira.security", "on_result": "block"},
                        {"builtin": "mira.secrets", "on_result": "report"},
                    ],
                },
                {"name": "review", "steps": [{"builtin": "mira.review"}]},
            ]
        )
        run = await run_workflow(
            wf,
            is_default=False,
            steps={"mira.security": gate, "mira.secrets": report},
            block_check=_block_blockers,
        )
        banner = render_workflow_banner(run)
        assert banner and "`mira.security`" in banner and "`review`" in banner
        section = render_workflow_section(run)
        assert section is not None
        assert "Stage `review`: skipped" in section
        assert "Cross-tenant read (`a.py:1`)" in section
        assert "`mira.secrets` findings (not posted)" in section
        assert "Noted" in section


# ── Engine wiring ─────────────────────────────────────────────────────────

_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+password = 'hunter2'\n"
    " y = 2\n"
)


def _engine_config(workflows: list[dict]) -> MiraConfig:
    cfg = MiraConfig.model_validate({"workflows": workflows})
    cfg.review.walkthrough = False
    cfg.review.self_critique = False
    cfg.review.include_summary = False
    cfg.review.pr_summary = "disable"
    return cfg


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_blocking_security_stage_skips_main_review(self, monkeypatch):
        from mira.core import engine as engine_mod
        from mira.core.engine import ReviewEngine

        async def fake_sec_pass(*a, **kw):
            c = _comment("SQL injection", Severity.BLOCKER, path="src/app.py")
            c.category = "security"
            return [c]

        review_calls: list[int] = []
        monkeypatch.setattr(engine_mod, "security_review_pass", fake_sec_pass)
        monkeypatch.setattr(
            engine_mod, "chunk_files", lambda *a, **kw: review_calls.append(1) or []
        )

        cfg = _engine_config(
            [
                {
                    "name": "secured",
                    "stages": [
                        {
                            "name": "gate",
                            "steps": [{"builtin": "mira.security", "on_result": "block"}],
                        },
                        {"name": "review", "steps": [{"builtin": "mira.review"}]},
                    ],
                }
            ]
        )
        engine = ReviewEngine(config=cfg, llm=AsyncMock(), provider=None)
        result = await engine._review_diff_internal(_DIFF)

        run = result.workflow_run
        assert run is not None and run.workflow == "secured" and not run.is_default
        assert run.blocked and run.skipped_stages == ["review"]
        assert [c.title for c in result.comments] == ["SQL injection"]
        # Chunking happens in prepare (shared by every step); the review step
        # itself never ran.
        review_step = next(s for s in run.steps if s.name == "mira.review")
        assert review_step.status == "skipped"

    @pytest.mark.asyncio
    async def test_report_step_findings_are_not_posted(self, monkeypatch):
        from mira.core import engine as engine_mod
        from mira.core.engine import ReviewEngine

        monkeypatch.setattr(engine_mod, "chunk_files", lambda *a, **kw: [])
        cfg = _engine_config(
            [
                {
                    "name": "report-secrets",
                    "stages": [
                        {
                            "name": "s",
                            "steps": [
                                {"builtin": "mira.review"},
                                {"builtin": "mira.secrets", "on_result": "report"},
                            ],
                        }
                    ],
                }
            ]
        )
        secret = _comment("Hardcoded password", Severity.WARNING, path="src/app.py")
        monkeypatch.setattr(engine_mod, "scan_secrets", AsyncMock(return_value=[secret]))
        engine = ReviewEngine(config=cfg, llm=AsyncMock(), provider=None)
        result = await engine._review_diff_internal(_DIFF)

        assert result.comments == []
        secrets_step = next(s for s in result.workflow_run.steps if s.name == "mira.secrets")
        assert [c.title for c in secrets_step.output.comments] == ["Hardcoded password"]

    @pytest.mark.asyncio
    async def test_unlisted_builtin_does_not_run(self, monkeypatch):
        from mira.core import engine as engine_mod
        from mira.core.engine import ReviewEngine

        sec = AsyncMock(return_value=[])
        monkeypatch.setattr(engine_mod, "security_review_pass", sec)
        monkeypatch.setattr(engine_mod, "chunk_files", lambda *a, **kw: [])
        cfg = _engine_config(
            [{"name": "main-only", "stages": [{"name": "s", "steps": [{"builtin": "mira.review"}]}]}]
        )
        engine = ReviewEngine(config=cfg, llm=AsyncMock(), provider=None)
        await engine._review_diff_internal(_DIFF)
        sec.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_workflow_config_uses_default(self, monkeypatch):
        from mira.core import engine as engine_mod
        from mira.core.engine import ReviewEngine

        sec = AsyncMock(return_value=[])
        monkeypatch.setattr(engine_mod, "security_review_pass", sec)
        monkeypatch.setattr(engine_mod, "chunk_files", lambda *a, **kw: [])
        engine = ReviewEngine(config=_engine_config([]), llm=AsyncMock(), provider=None)
        result = await engine._review_diff_internal(_DIFF)
        sec.assert_awaited_once()
        assert result.workflow_run.is_default
        assert render_workflow_section(result.workflow_run) is None


class TestCheckStatus:
    @pytest.mark.asyncio
    async def test_blocked_run_fails_the_check(self, monkeypatch):
        from mira.core.engine import ReviewEngine
        from mira.models import ReviewResult
        from mira.workflow.runner import StageRun, StepRun, WorkflowRun

        step = StepConfig(builtin="mira.security", on_result="block")
        run = WorkflowRun(
            workflow="secured",
            is_default=False,
            stages=[
                StageRun(
                    name="gate",
                    steps=[
                        StepRun(
                            stage="gate",
                            step=step,
                            status="blocked",
                            blocking=[_comment("x", Severity.BLOCKER)],
                        )
                    ],
                )
            ],
        )
        provider = AsyncMock()
        provider.get_pr_info.return_value = AsyncMock(owner="acme", repo="api")
        engine = ReviewEngine(config=_engine_config([]), llm=AsyncMock(), provider=provider)
        monkeypatch.setattr(engine, "_start_check", AsyncMock(return_value=1))
        finish = AsyncMock()
        monkeypatch.setattr(engine, "_finish_check", finish)
        monkeypatch.setattr(
            engine, "_review_pr", AsyncMock(return_value=ReviewResult(workflow_run=run))
        )
        await engine.review_pr("https://example.test/pr/1")
        assert finish.await_args.args[2] == "blocked"
        assert ReviewEngine._CHECK_CONCLUSIONS["blocked"][0] == "failure"
