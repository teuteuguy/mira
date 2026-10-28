"""Tests for the repository allow/block filter and its use in every dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import FilterConfig, MiraConfig
from mira.platforms.bitbucket import webhook as bb_webhook
from mira.platforms.forgejo import webhook as fj_webhook
from mira.platforms.github import webhook as gh_webhook
from mira.platforms.gitlab import webhook as gl_webhook
from mira.platforms.mentions import repo_is_filtered

BOT = "mira-bot"


# ── repo_is_filtered ──


def test_empty_lists_allow_everything() -> None:
    assert not repo_is_filtered("acme", "api", [], [])


def test_allowed_glob_matches() -> None:
    assert not repo_is_filtered("acme", "api", ["acme/*"], [])
    assert repo_is_filtered("other", "api", ["acme/*"], [])


def test_glob_on_repo_part() -> None:
    assert not repo_is_filtered("acme", "mira-test-1", ["*/mira-test-*"], [])
    assert repo_is_filtered("acme", "prod-api", ["*/mira-test-*"], [])


def test_exact_name_match() -> None:
    assert not repo_is_filtered("acme", "api", ["acme/api"], [])
    assert repo_is_filtered("acme", "api2", ["acme/api"], [])


def test_case_insensitive() -> None:
    assert not repo_is_filtered("Acme", "API", ["acme/api"], [])
    assert not repo_is_filtered("acme", "api", ["ACME/*"], [])
    assert repo_is_filtered("ACME", "Api", [], ["acme/API"])


def test_nested_gitlab_owner() -> None:
    assert not repo_is_filtered("group/sub", "proj", ["group/sub/*"], [])
    assert not repo_is_filtered("group/sub", "proj", ["group/*"], [])
    assert repo_is_filtered("group/sub", "proj", ["other/*"], [])


def test_blocked_wins_over_allowed() -> None:
    assert repo_is_filtered("acme", "secret", ["acme/*"], ["acme/secret"])
    assert not repo_is_filtered("acme", "api", ["acme/*"], ["acme/secret"])


def test_blocked_only() -> None:
    assert repo_is_filtered("acme", "legacy-x", [], ["*/legacy-*"])
    assert not repo_is_filtered("acme", "api", [], ["*/legacy-*"])


def test_empty_repo_never_filtered() -> None:
    assert not repo_is_filtered("", "", ["acme/*"], ["*"])


def test_project_key_matches_case_insensitively() -> None:
    assert not repo_is_filtered("ws", "repo", ["project:CLOUD"], [], project_key="cloud")
    assert repo_is_filtered("ws", "repo", ["project:CLOUD"], [], project_key="FW")
    assert repo_is_filtered("ws", "repo", [], ["Project:fw"], project_key="FW")


def test_project_key_absent_never_matches() -> None:
    # Without a project key (GitHub/GitLab/Forgejo) `project:` patterns are inert.
    assert repo_is_filtered("ws", "repo", ["project:CLOUD"], [])
    assert not repo_is_filtered("ws", "repo", [], ["project:CLOUD"])


def test_project_and_glob_patterns_mix() -> None:
    allowed = ["project:CLOUD", "ws/tools-*"]
    assert not repo_is_filtered("ws", "tools-x", allowed, [], project_key="FW")
    assert not repo_is_filtered("ws", "svc", allowed, [], project_key="CLOUD")
    assert repo_is_filtered("ws", "svc", allowed, [], project_key="FW")


# ── dispatchers ──


def _cfg(**kw: list[str]) -> MiraConfig:
    return MiraConfig(filter=FilterConfig(**kw))


def _scheduled(bg: MagicMock) -> list:
    return [c.args[0] for c in bg.add_task.call_args_list]


def _gh_pr(owner: str = "acme", repo: str = "api") -> dict:
    return {
        "action": "opened",
        "installation": {"id": 1},
        "sender": {"login": "alice"},
        "pull_request": {"number": 1, "body": "", "labels": []},
        "repository": {"owner": {"login": owner}, "name": repo, "full_name": f"{owner}/{repo}"},
    }


@pytest.fixture
def gh_auth() -> MagicMock:
    auth = MagicMock()
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    return auth


async def test_github_blocked_repo_pr_ignored(gh_auth: MagicMock) -> None:
    bg = MagicMock()
    with patch.object(gh_webhook, "load_config", return_value=_cfg(blocked_repos=["acme/*"])):
        status = await gh_webhook.dispatch_github_event("pull_request", _gh_pr(), gh_auth, BOT, bg)
    assert status == "ignored"
    bg.add_task.assert_not_called()


async def test_github_allowed_repo_pr_processed(gh_auth: MagicMock) -> None:
    bg = MagicMock()
    with patch.object(gh_webhook, "load_config", return_value=_cfg(allowed_repos=["acme/*"])):
        status = await gh_webhook.dispatch_github_event("pull_request", _gh_pr(), gh_auth, BOT, bg)
    assert status == "processing"
    assert gh_webhook.handle_pull_request in _scheduled(bg)


async def test_github_off_allowlist_comment_and_push_ignored(gh_auth: MagicMock) -> None:
    cfg = _cfg(allowed_repos=["acme/*"])
    comment = {
        "action": "created",
        "comment": {"body": "@mira-bot review", "user": {"login": "alice", "type": "User"}},
        "issue": {"number": 1, "pull_request": {}},
        "repository": {"owner": {"login": "other"}, "name": "api"},
    }
    push = {
        "ref": "refs/heads/main",
        "sender": {"login": "alice"},
        "repository": {"owner": {"login": "other"}, "name": "api", "default_branch": "main"},
    }
    bg = MagicMock()
    with patch.object(gh_webhook, "load_config", return_value=cfg):
        assert (
            await gh_webhook.dispatch_github_event("issue_comment", comment, gh_auth, BOT, bg)
            == "ignored"
        )
        assert await gh_webhook.dispatch_github_event("push", push, gh_auth, BOT, bg) == "ignored"
    bg.add_task.assert_not_called()


async def test_github_installation_events_not_filtered(gh_auth: MagicMock) -> None:
    bg = MagicMock()
    payload = {"action": "created", "installation": {"id": 1}, "repositories": []}
    with patch.object(gh_webhook, "load_config", return_value=_cfg(blocked_repos=["*"])):
        status = await gh_webhook.dispatch_github_event("installation", payload, gh_auth, BOT, bg)
    assert status == "processing"
    assert _scheduled(bg) == [gh_webhook.handle_installation]


@pytest.fixture
def plain_auth() -> MagicMock:
    auth = MagicMock(spec=["get_bot_identity"])
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    return auth


async def test_gitlab_blocked_repo_mr_ignored(plain_auth: MagicMock) -> None:
    payload = {
        "object_attributes": {"iid": 7, "action": "open"},
        "project": {"path_with_namespace": "group/sub/proj"},
        "user": {"username": "alice"},
    }
    bg = MagicMock()
    with patch.object(gl_webhook, "load_config", return_value=_cfg(blocked_repos=["group/sub/*"])):
        status = await gl_webhook.dispatch_gitlab_event(
            "Merge Request Hook", payload, plain_auth, BOT, bg
        )
    assert status == "ignored"
    bg.add_task.assert_not_called()


async def test_gitlab_allowed_repo_mr_processed(plain_auth: MagicMock) -> None:
    payload = {
        "object_attributes": {"iid": 7, "action": "open"},
        "project": {"path_with_namespace": "group/sub/proj"},
        "user": {"username": "alice"},
    }
    bg = MagicMock()
    with patch.object(gl_webhook, "load_config", return_value=_cfg(allowed_repos=["group/*"])):
        status = await gl_webhook.dispatch_gitlab_event(
            "Merge Request Hook", payload, plain_auth, BOT, bg
        )
    assert status == "processing"
    assert _scheduled(bg) == [gl_webhook.handle_merge_request]


async def test_forgejo_blocked_repo_pr_ignored(plain_auth: MagicMock) -> None:
    payload = {
        "action": "opened",
        "sender": {"login": "alice"},
        "pull_request": {"number": 1},
        "repository": {"full_name": "acme/api"},
    }
    bg = MagicMock()
    with patch.object(fj_webhook, "load_config", return_value=_cfg(blocked_repos=["acme/api"])):
        status = await fj_webhook.dispatch_forgejo_event(
            "pull_request", payload, plain_auth, BOT, bg
        )
    assert status == "ignored"
    bg.add_task.assert_not_called()


@pytest.fixture
def bb_auth() -> MagicMock:
    auth = MagicMock()
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    auth.get_bot_ids = AsyncMock(return_value={"mira-bot"})
    return auth


def _bb_comment(project_key: str | None) -> dict:
    repository: dict = {"full_name": "interelgit/svc"}
    if project_key is not None:
        repository["project"] = {"key": project_key}
    return {
        "actor": {"nickname": "alice", "account_id": "557058:alice"},
        "pullrequest": {"id": 3},
        "comment": {"content": {"raw": "@mira-bot review"}},
        "repository": repository,
    }


async def test_bitbucket_blocked_project_comment_ignored(bb_auth: MagicMock) -> None:
    bg = MagicMock()
    with patch.object(bb_webhook, "load_config", return_value=_cfg(blocked_repos=["project:FW"])):
        status = await bb_webhook.dispatch_bitbucket_event(
            "pullrequest:comment_created", _bb_comment("fw"), bb_auth, BOT, bg
        )
    assert status == "ignored"
    bg.add_task.assert_not_called()


async def test_bitbucket_allowed_project_comment_processed(bb_auth: MagicMock) -> None:
    bg = MagicMock()
    cfg = _cfg(allowed_repos=["project:CLOUD"])
    with patch.object(bb_webhook, "load_config", return_value=cfg):
        status = await bb_webhook.dispatch_bitbucket_event(
            "pullrequest:comment_created", _bb_comment("CLOUD"), bb_auth, BOT, bg
        )
    assert status == "processing"
    assert _scheduled(bg) == [bb_webhook.handle_bitbucket_comment]


async def test_bitbucket_off_allowlist_push_ignored(bb_auth: MagicMock) -> None:
    payload = {
        "actor": {"nickname": "alice", "account_id": "557058:alice"},
        "push": {"changes": []},
        "repository": {"full_name": "interelgit/svc"},
    }
    bg = MagicMock()
    cfg = _cfg(allowed_repos=["project:CLOUD"])
    with patch.object(bb_webhook, "load_config", return_value=cfg):
        status = await bb_webhook.dispatch_bitbucket_event("repo:push", payload, bb_auth, BOT, bg)
    assert status == "ignored"
    bg.add_task.assert_not_called()
