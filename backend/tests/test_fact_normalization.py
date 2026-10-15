from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devflow.sync.schemas import SyncTarget


def test_number_only_pr_hint_never_uses_issue_identity():
    assert SyncTarget(kind="pull", number=7).source_id is None
    for value in (
        {"kind": "issue", "number": 7},
        {"kind": "pull", "source_id": 77},
        {"kind": "ci_run", "source_id": 77, "number": 7},
        {"kind": "issue", "source_id": 77, "number": 7, "attempt": 2},
    ):
        with pytest.raises(ValidationError):
            SyncTarget(**value)


def test_normalization_whitelist_and_reasons():
    from devflow.facts.normalize import FactContext, normalize_fact

    repo = uuid4()
    value = normalize_fact(
        "issue",
        {
            "id": 51,
            "number": 7,
            "title": "title",
            "state": "future",
            "body": "x" * 70000,
            "labels": [{"name": "bug", "secret": "no"}],
            "assignees": [{"id": 6, "login": "dev", "token": "no"}],
            "user": None,
            "repository_id": str(uuid4()),
            "token": "no",
        },
        FactContext(
            repository_id=repo, observed_at=datetime.now(UTC), full_name="team/private"
        ),
    )
    assert value.repository_id == repo
    assert value.payload.state == "unknown"
    assert len(value.payload.body) == 65536
    assert set(value.payload.normalization_reasons) == {
        "unknown_value",
        "content_truncated",
        "field_unavailable",
    }
    assert value.payload.labels == ["bug"]
    assert value.payload.assignees[0].model_dump() == {"id": 6, "login": "dev"}
    assert "token" not in value.model_dump_json()


def test_review_retains_old_commit_and_position():
    from devflow.facts.normalize import FactContext, normalize_fact

    fact = normalize_fact(
        "review_comment",
        {
            "id": 90,
            "body": "old",
            "commit_id": "a" * 40,
            "original_commit_id": "b" * 40,
            "pull_request_review_id": 80,
            "path": "Case/A.py",
            "side": "RIGHT",
            "line": None,
            "original_line": 4,
        },
        FactContext(
            repository_id=uuid4(),
            observed_at=datetime.now(UTC),
            full_name="team/private",
            parent_kind="pull",
            parent_source_id=77,
        ),
    )
    assert fact.payload.commit_id == "a" * 40
    assert fact.payload.original_line == 4 and fact.payload.line is None
    assert fact.payload.review_id == 80


def test_context_owns_pull_version_and_file_link_uses_number():
    from devflow.facts.normalize import FactContext, normalize_fact

    c = FactContext(
        repository_id=uuid4(),
        observed_at=datetime.now(UTC),
        full_name="team/private",
        issue_id=55,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    value = normalize_fact(
        "pull",
        {
            "id": 70,
            "number": 7,
            "title": "t",
            "state": "open",
            "draft": False,
            "merged": False,
            "base": {"sha": "c" * 40},
            "head": {"sha": "d" * 40},
        },
        c,
    )
    assert value.payload.base_sha == c.base_sha and value.payload.head_sha == c.head_sha
    c = FactContext(
        repository_id=c.repository_id,
        observed_at=c.observed_at,
        full_name=c.full_name,
        parent_kind="pull",
        parent_source_id=70,
        parent_number=7,
        base_sha=c.base_sha,
        head_sha=c.head_sha,
    )
    value = normalize_fact(
        "pr_file",
        {
            "filename": "A.py",
            "status": "added",
            "additions": 1,
            "deletions": 0,
            "changes": 1,
        },
        c,
    )
    assert value.source_url == "https://github.com/team/private/pull/7/files"
