"""Walkthrough verdict when nothing is posted, and the unverified-findings section."""

from __future__ import annotations

from mira.models import WalkthroughConfidenceScore, WalkthroughResult


def _wt() -> WalkthroughResult:
    return WalkthroughResult(
        summary="Adds a thing.",
        confidence_score=WalkthroughConfidenceScore(
            score=4, label="Safe with minor fixes", reason="why"
        ),
    )


def test_no_comments_gives_explicit_verdict_and_consistent_label():
    md = _wt().to_markdown(reviewed_files=13, total_comments=0, dismissed_findings=2)
    assert "✅ **No issues to flag.** 2 candidate findings dismissed by Mira's self-check." in md
    assert "No issues flagged" in md
    assert "Safe with minor fixes" not in md


def test_singular_and_zero_dismissed():
    assert "1 candidate finding dismissed" in _wt().to_markdown(
        total_comments=0, dismissed_findings=1
    )
    md = _wt().to_markdown(total_comments=0, dismissed_findings=0)
    assert "✅ **No issues to flag.**" in md
    assert "dismissed" not in md


def test_callers_without_critique_results_are_unchanged():
    """Placeholder, failure and other callers don't pass dismissed_findings."""
    md = _wt().to_markdown()
    assert "Safe with minor fixes" in md
    assert "No issues to flag" not in md
    failed = _wt().to_markdown(dismissed_findings=0, failure_notice="Review failed")
    assert "No issues to flag" not in failed


def test_with_comments_keeps_label_and_no_verdict():
    md = _wt().to_markdown(reviewed_files=3, total_comments=2, dismissed_findings=1)
    assert "Safe with minor fixes" in md
    assert "No issues to flag" not in md


def test_unverified_findings_section_is_capped_and_marked():
    findings = [{"path": f"src/f{i}.ts", "line": i, "title": f"maybe {i}"} for i in range(7)]
    md = _wt().to_markdown(total_comments=0, unverified_findings=findings)
    assert "<summary><b>Worth a look (not verified)</b> — 7</summary>" in md
    assert "- `src/f0.ts:0` — maybe 0" in md
    assert "src/f5.ts" not in md
    assert "…and 2 more" in md
    assert "check them yourself" in md


def test_in_progress_shows_neither():
    md = _wt().to_markdown(
        in_progress=True, unverified_findings=[{"path": "a", "line": 1, "title": "t"}]
    )
    assert "Worth a look" not in md
    assert "No issues to flag" not in md
