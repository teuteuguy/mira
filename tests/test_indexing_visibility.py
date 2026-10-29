"""Indexing progress reporting, indexing reasoning effort, and resume-on-start."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig, MiraConfig
from mira.dashboard.models_config import llm_config_for
from mira.index.indexer import index_repo
from mira.index.status import IndexingTracker
from mira.index.store import IndexStore
from tests.test_index_indexer import _FakeFetcher


def _summary(path: str) -> dict:
    return {
        "path": path,
        "language": "python",
        "summary": f"Summary of {path}.",
        "symbols": [],
        "imports": [],
        "symbol_references": [],
    }


@pytest.mark.asyncio
async def test_index_repo_reports_progress(tmp_path):
    store = IndexStore(str(tmp_path / "test.db"))
    big = "print('x')\n" * 100
    paths = [f"src/m{i}.py" for i in range(3)]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps({"files": [_summary(p) for p in paths]}))
    calls: list[tuple[int, int]] = []

    await index_repo(
        owner="o",
        repo="r",
        config=MiraConfig(),
        store=store,
        llm=llm,
        full=True,
        fetcher=_FakeFetcher(tree=paths, tarball=dict.fromkeys(paths, big)),
        on_progress=lambda done, total: calls.append((done, total)),
    )
    store.close()
    assert calls[0] == (0, 3)
    assert calls[-1] == (3, 3)
    assert all(total == 3 for _, total in calls)


def test_tracker_progress_sets_total():
    tracker = IndexingTracker()
    tracker.start("o/r")
    tracker.progress("o/r", 5, 40)
    job = tracker._jobs["o/r"]
    assert (job.files_done, job.files_total) == (5, 40)
    tracker.progress("o/r", 9)
    assert (job.files_done, job.files_total) == (9, 40)


class TestIndexingReasoningEffort:
    def _resolve(self, **kw) -> LLMConfig:
        with patch("mira.dashboard.api._app_db", None):
            return llm_config_for("indexing", LLMConfig(indexing_model="cheap/model", **kw))

    def test_applies_to_indexing(self):
        assert self._resolve(indexing_reasoning_effort="low").reasoning_effort == "low"

    def test_unset_or_off_sends_none(self):
        assert self._resolve().reasoning_effort is None
        assert self._resolve(indexing_reasoning_effort="off").reasoning_effort is None

    def test_does_not_leak_into_reviews(self):
        with patch("mira.dashboard.api._app_db", None):
            cfg = llm_config_for("review", LLMConfig(indexing_reasoning_effort="low"))
        assert cfg.reasoning_effort is None


@pytest.mark.asyncio
async def test_resume_only_picks_repos_stuck_indexing():
    from mira.dashboard import api

    def repo(name: str, status: str) -> MagicMock:
        r = MagicMock(owner="o", repo=name, platform="bitbucket", status=status, index_mode="full")
        return r

    db = MagicMock()
    db.list_repos.return_value = [repo("a", "indexing"), repo("b", "pending"), repo("c", "ready")]
    indexed: list[str] = []

    async def fake_index_repo(**kw):
        indexed.append(kw["repo"])
        return 1

    store = MagicMock()
    store.all_paths.return_value = {"x"}
    with (
        patch.object(api, "_app_db", db),
        patch.dict("os.environ", {"MIRA_BITBUCKET_TOKEN": "tok"}),
        patch("mira.index.indexer.index_repo", new=fake_index_repo),
        patch("mira.llm.create_llm", return_value=MagicMock()),
        patch.object(api, "IndexStore") as store_cls,
        patch("mira.outbound_webhooks.dispatch_event", new=AsyncMock()),
    ):
        store_cls.open.return_value = store
        await api.resume_interrupted_indexing()
    assert indexed == ["a"]
