"""Explicit retrieval scope: user selection, authorization and DB generations.

The fixtures use real PostgreSQL and repository policy. They make the scope
contract observable without claiming retrieval quality or Milvus acceptance.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from support.index_env import NEXT_SHA, SHA, make_index, ready_import, set_active
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import installation, repository

from devflow.errors import AppError
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    IndexVersion,
    RevisionDependency,
)
from devflow.knowledge.retrieval import RetrievalService
from devflow.knowledge.schemas import IndexSelection, QuerySelection
from devflow.repositories.models import Repository

pytestmark = pytest.mark.asyncio

EXTRAS = {
    202: "other/fork",
    303: "team/third",
    404: "team/fourth",
}


def service(env) -> RetrievalService:
    return RetrievalService(env.factory, env.policy)


async def register(env, numbers):
    """Register extra team repositories through the real policy boundary."""
    env.installations = {1: ([installation(), installation(12)], None)}
    env.repositories[(12, 1)] = (
        [repository(number, name) for number, name in EXTRAS.items()],
        None,
    )
    local = {}
    for number in numbers:
        view = await env.service.register(env.actor, 12, number)
        local[number] = view.id
    return local


async def ready_corpus(env, *, generation=None, sha=SHA):
    fixture = await ready_import(env, generation=generation, sha=sha)
    row = await make_index(env, fixture, status="ready")
    await set_active(env, row.id)
    return fixture, row


async def fork_source(env, fixture, fork_id):
    """Make the base index's only member originate from the fork repository."""
    async with env.factory.begin() as db:
        revision_id = fixture.revision_ids[0]
        document_id = await db.scalar(
            select(DocumentRevision.document_id).where(
                DocumentRevision.id == revision_id
            )
        )
        document = await db.get(Document, document_id, with_for_update=True)
        document.source_repository_id = fork_id
        # ready_import already records the base repository dependency.
        db.add(RevisionDependency(revision_id=revision_id, repository_id=fork_id))


async def set_repository_enabled(env, repository_id, enabled):
    async with env.factory.begin() as db:
        row = await db.get(Repository, repository_id, with_for_update=True)
        row.enabled = enabled


async def set_index_status(env, index_id, status, *, queryable=False):
    async with env.factory.begin() as db:
        row = await db.get(IndexVersion, index_id, with_for_update=True)
        row.status = status
        row.queryable = queryable
        if status == "retired":
            row.retired_at = datetime.now(UTC)


def selector(repository_id, sha=SHA, target="target"):
    return IndexSelection(repository_id=repository_id, sha=sha, target=target)


async def test_fork_authorization_does_not_expand_selection(knowledge_env):
    env = knowledge_env
    fork_id = (await register(env, [202]))[202]
    fixture, _ = await ready_corpus(env)
    await fork_source(env, fixture, fork_id)

    base = selector(env.repository.id)
    only_base = await service(env).freeze(
        env.actor,
        QuerySelection(selected_repository_ids=(env.repository.id,), indexes=(base,)),
    )
    # Having fork permission must not silently add the fork source.
    assert only_base.selected_repository_ids == (env.repository.id,)
    assert fork_id not in only_base.selected_repository_ids
    assert [item.repository_id for item in only_base.indexes] == [env.repository.id]

    both_selection = QuerySelection(
        selected_repository_ids=(env.repository.id, fork_id), indexes=(base,)
    )
    both = await service(env).freeze(env.actor, both_selection)
    assert set(both.selected_repository_ids) == {env.repository.id, fork_id}
    # A fork source may participate without owning an index of its own.
    assert [item.repository_id for item in both.indexes] == [env.repository.id]

    # Either side losing access rejects the entire frozen scope.
    await set_repository_enabled(env, fork_id, False)
    with pytest.raises(AppError) as error:
        await service(env).freeze(env.actor, both_selection)
    assert error.value.code == "resource_not_found"
    with pytest.raises(AppError) as error:
        await service(env).revalidate(both)
    assert error.value.code == "resource_not_found"

    await set_repository_enabled(env, fork_id, True)
    await set_repository_enabled(env, env.repository.id, False)
    with pytest.raises(AppError) as error:
        await service(env).freeze(
            env.actor,
            QuerySelection(
                selected_repository_ids=(env.repository.id,), indexes=(base,)
            ),
        )
    assert error.value.code == "resource_not_found"


async def test_target_generation_not_sha_is_freshness(knowledge_env):
    env = knowledge_env
    fixture1 = await ready_import(env, generation=1, sha=SHA)
    first = await make_index(env, fixture1, status="ready")
    await set_active(env, first.id)

    async def target(sha):
        return await service(env).freeze(
            env.actor,
            QuerySelection(
                selected_repository_ids=(env.repository.id,),
                indexes=(selector(env.repository.id, sha, "target"),),
            ),
        )

    async def active(sha):
        return await service(env).freeze(
            env.actor,
            QuerySelection(
                selected_repository_ids=(env.repository.id,),
                indexes=(selector(env.repository.id, sha, "active"),),
            ),
        )

    # The same SHA in a newer target generation is not the old published index.
    fixture2 = await ready_import(env, generation=2, sha=SHA)
    pending = await make_index(env, fixture2, status="building")
    with pytest.raises(AppError) as error:
        await target(SHA)
    assert error.value.code == "index_not_ready"

    # A newer SHA target being built does not hide the explicitly selected
    # older active index, but the default target never falls back to it.
    await set_index_status(env, pending.id, "superseded")
    fixture3 = await ready_import(env, generation=3, sha=NEXT_SHA)
    building = await make_index(env, fixture3, status="building")
    scope = await active(SHA)
    frozen = scope.indexes[0]
    assert frozen.index_version_id == first.id
    assert frozen.sha == SHA
    assert frozen.index_generation == 1
    assert frozen.target_generation == 3

    with pytest.raises(AppError) as error:
        await target(NEXT_SHA)
    assert error.value.code == "index_not_ready"
    with pytest.raises(AppError) as error:
        await target(SHA)
    assert error.value.code == "version_changed"

    # A retired version cannot back a newly frozen scope or be revalidated.
    await set_index_status(env, first.id, "retired")
    with pytest.raises(AppError) as error:
        await active(SHA)
    assert error.value.code == "index_not_ready"
    with pytest.raises(AppError) as error:
        await service(env).revalidate(scope)
    assert error.value.code == "version_changed"
    assert building.status == "building"


async def test_scope_three_limit_counts_source_only_repositories(knowledge_env):
    env = knowledge_env
    ids = await register(env, [202, 303, 404])
    await ready_corpus(env)
    base = selector(env.repository.id)

    scope = await service(env).freeze(
        env.actor,
        QuerySelection(
            selected_repository_ids=(
                env.repository.id,
                ids[202],
                ids[303],
            ),
            indexes=(base,),
        ),
    )
    # Three repositories including source-only participants are allowed; only
    # the index owner is frozen as an index.
    assert len(scope.selected_repository_ids) == 3
    assert [item.repository_id for item in scope.indexes] == [env.repository.id]

    with pytest.raises(ValidationError):
        QuerySelection(
            selected_repository_ids=(
                env.repository.id,
                ids[202],
                ids[303],
                ids[404],
            ),
            indexes=(base,),
        )
    with pytest.raises(ValidationError):
        QuerySelection(selected_repository_ids=(), indexes=(base,))
    with pytest.raises(ValidationError):
        QuerySelection(selected_repository_ids=(env.repository.id,), indexes=())
    with pytest.raises(ValidationError):
        QuerySelection(
            selected_repository_ids=(env.repository.id, env.repository.id),
            indexes=(base,),
        )
    with pytest.raises(ValidationError):
        QuerySelection(
            selected_repository_ids=(env.repository.id,), indexes=(base, base)
        )
    # An index owner must be part of the explicit selection, not implied by an
    # installation grant.
    with pytest.raises(ValidationError):
        QuerySelection(selected_repository_ids=(ids[202], ids[303]), indexes=(base,))
    # No arbitrary index UUID, filter or epoch channel exists in the DTO.
    with pytest.raises(ValidationError):
        QuerySelection.model_validate(
            {
                "selected_repository_ids": [str(env.repository.id)],
                "indexes": [
                    {
                        "repository_id": str(env.repository.id),
                        "sha": SHA,
                        "target": "target",
                        "index_version_id": "00000000-0000-0000-0000-000000000000",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        QuerySelection.model_validate(
            {
                "selected_repository_ids": [str(env.repository.id)],
                "indexes": [
                    {
                        "repository_id": str(env.repository.id),
                        "sha": SHA,
                        "target": "fingerprint",
                    }
                ],
            }
        )
