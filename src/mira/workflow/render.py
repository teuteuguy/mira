"""Walkthrough markdown for a workflow run."""

from __future__ import annotations

from mira.workflow.config import OUTPUT_STEPS
from mira.workflow.runner import StepRun, WorkflowRun

_SEVERITY_ICON = {"BLOCKER": "🔴", "WARNING": "🟡", "SUGGESTION": "🔵", "NITPICK": "⚪"}
_MAX_LISTED = 5


def render_workflow_banner(run: WorkflowRun | None) -> str | None:
    """A one-paragraph notice at the top of the walkthrough when a step blocked."""
    if run is None or not run.blocked:
        return None
    names = ", ".join(f"`{s.name}`" for s in run.blocked_by)
    lines = [f"> ⛔ **Blocked by {names}.**"]
    if run.skipped_stages:
        skipped = ", ".join(f"`{n}`" for n in run.skipped_stages)
        lines.append(f"> Skipped stages: {skipped}.")
    lines.append("> Fix the blocking findings and push, or comment `@mira review` to run again.")
    return "\n".join(lines)


def _step_line(sr: StepRun) -> str:
    if sr.status == "skipped":
        reason = f": {sr.skipped_reason}" if sr.skipped_reason else ""
        return f"⏭ `{sr.name}` skipped{reason}"
    if sr.status == "error":
        return f"⚠️ `{sr.name}` failed ({sr.error or 'error'})"
    count = len(sr.output.comments)
    findings = f"{count} finding{'s' if count != 1 else ''}"
    mode = sr.step.result_mode
    suffix = " (report only)" if mode == "report" else ""
    if sr.status == "blocked":
        if sr.error:
            return f"⛔ `{sr.name}` blocked: failed ({sr.error})"
        return f"⛔ `{sr.name}` blocked: {len(sr.blocking)} blocking of {findings}{suffix}"
    return f"✅ `{sr.name}`: {findings}{suffix}"


def render_workflow_section(run: WorkflowRun | None) -> str | None:
    """List each stage and step of a custom workflow, plus report-only findings.

    Returns ``None`` for the built-in default workflow, so instances without
    workflow config keep their exact walkthrough.
    """
    if run is None or run.is_default:
        return None
    lines = [f"**Workflow `{run.workflow}`**", ""]
    n = 0
    for stage in run.stages:
        n += 1
        if stage.skipped:
            lines.append(f"{n}. ⏭ Stage `{stage.name}`: skipped (an earlier stage blocked)")
            continue
        lines.append(f"{n}. Stage `{stage.name}`")
        m = 0
        for sr in stage.steps:
            if sr.step.builtin in OUTPUT_STEPS:
                continue
            m += 1
            lines.append(f"    {m}. {_step_line(sr)}")

    for sr in run.steps:
        listed = sr.blocking if sr.status == "blocked" else []
        if sr.step.result_mode == "report" and sr.status in ("passed", "blocked"):
            listed = sr.output.comments
        if not listed:
            continue
        title = "blocking findings" if sr.status == "blocked" else "findings (not posted)"
        lines.append("")
        lines.append(f"**`{sr.name}` {title}**")
        lines.append("")
        for i, c in enumerate(listed[:_MAX_LISTED], 1):
            icon = _SEVERITY_ICON.get(c.severity.name, "")
            lines.append(f"{i}. {icon} {c.title} (`{c.path}:{c.line}`)")
        if len(listed) > _MAX_LISTED:
            lines.append(f"{_MAX_LISTED + 1}. …and {len(listed) - _MAX_LISTED} more")
    return "\n".join(lines)
