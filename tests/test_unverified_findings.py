"""Opt-in "Worth a look (not verified)" walkthrough section (review.show_unverified_findings)."""

from __future__ import annotations

from mira.config import MiraConfig
from mira.core.engine import _clamp_confidence_to_findings, _unverified_findings
from mira.models import WalkthroughConfidenceScore, WalkthroughResult


def _wt(score: int = 4) -> WalkthroughResult:
    return WalkthroughResult(
        summary="Adds a thing.",
        confidence_score=WalkthroughConfidenceScore(
            score=score, label="Safe with minor fixes", reason="why"
        ),
    )


def test_off_by_default():
    assert MiraConfig().review.show_unverified_findings is False


def test_only_plausible_self_critique_drops_are_listed():
    audit = [
        {"stage": "drafted", "chunk": 0, "count": 3},
        {"stage": "noise_filter", "path": "a.py", "line": 1, "title": "nit"},
        {
            "stage": "self_critique",
            "path": "b.py",
            "line": 2,
            "title": "maybe",
            "reason": "plausible: x",
        },
        {
            "stage": "self_critique",
            "path": "c.py",
            "line": 3,
            "title": "wrong",
            "reason": "unsupported: y",
        },
    ]
    assert [f["path"] for f in _unverified_findings(audit)] == ["b.py"]


def test_label_when_unverified_findings_are_shown():
    wt = _wt()
    _clamp_confidence_to_findings(wt, [], unverified=2)
    assert wt.confidence_score.label == "No confirmed issues"
    plain = _wt()
    _clamp_confidence_to_findings(plain, [], unverified=0)
    assert plain.confidence_score.label == "No issues to flag"


def test_section_is_collapsed_capped_and_marked_unverified():
    findings = [{"path": f"src/f{i}.ts", "line": i, "title": f"maybe {i}"} for i in range(7)]
    md = _wt().to_markdown(total_comments=0, unverified_findings=findings)
    assert "<details>\n<summary><b>Worth a look (not verified)</b> — 7</summary>" in md
    assert "- `src/f0.ts:0` — maybe 0" in md
    assert "src/f5.ts" not in md
    assert "…and 2 more" in md
    assert "check them yourself" in md


def test_absent_when_not_provided_or_in_progress():
    assert "Worth a look" not in _wt().to_markdown()
    in_progress = _wt().to_markdown(
        in_progress=True, unverified_findings=[{"path": "a", "line": 1, "title": "t"}]
    )
    assert "Worth a look" not in in_progress
