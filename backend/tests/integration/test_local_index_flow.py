"""Published-index restart and incomplete-restore behaviour on real Milvus.

These tests only run when ``TEST_MILVUS_URI`` points at an isolated test
service. The embedding fixture is deterministic (model quality is never
claimed here); the Milvus schema, rows, Strong visibility checks and searches
are real. Real model inference is a separate 3B gate in
``test_model_runtime.py`` and is not implied by this file.

An actual Milvus *server* restart additionally requires
``TEST_MILVUS_RESTART_COMMAND``. Without it that case is NOT EXECUTED rather
than downgraded to a client reconnect under the same label.
"""

import asyncio
import os
import shlex
import subprocess
import time
from contextlib import suppress
from uuid import uuid4

import pytest
from sqlalchemy import select
from support.index_env import (
    FakeInference,
    fingerprint,
    ready_import,
)
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

from devflow.config import Settings
from devflow.errors import AppError
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import MODEL_JOB_KINDS
from devflow.knowledge.indexing import IndexAvailabilityService, IndexBuilder
from devflow.knowledge.milvus import (
    DIMENSION,
    IndexFilter,
    IndexFingerprint,
    MilvusIndex,
)
from devflow.knowledge.models import IndexMember, IndexVersion, KnowledgeCorpus
from devflow.knowledge.service import KnowledgeService

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_MILVUS_URI"),
        reason="TEST_MILVUS_URI is not set; real Milvus index flow NOT EXECUTED",
    ),
]

MILVUS_URI = os.environ.get("TEST_MILVUS_URI")
RESTART_COMMAND = os.environ.get("TEST_MILVUS_RESTART_COMMAND")


def make_index(collection: str) -> tuple[MilvusIndex, object]:
    from pymilvus import MilvusClient

    settings = Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://localhost:3000",
        milvus_uri=MILVUS_URI,
        milvus_collection=collection,
        milvus_timeout_seconds=5,
        milvus_build_timeout_seconds=120,
    )
    client = MilvusClient(uri=MILVUS_URI, timeout=5)
    return MilvusIndex(settings, client=client), client


def close_index(client, collection: str) -> None:
    with suppress(Exception):
        client.drop_collection(collection)
    with suppress(Exception):
        client.close()


def collection_present(collection: str) -> bool:
    from pymilvus import MilvusClient

    client = MilvusClient(uri=MILVUS_URI, timeout=5)
    try:
        return client.has_collection(collection)
    except Exception:  # noqa: BLE001 - a restarting server can raise any transport error
        return False
    finally:
        with suppress(Exception):
            client.close()


async def publish_small(env, index: MilvusIndex):
    fixture = await ready_import(env, chunk_count=4)
    build = IndexBuilder(
        KnowledgeService(env.factory, env.policy),
        inference=FakeInference(),
        index=index,
    )
    receipt = await build.request(env.actor, fixture.import_id, f"flow-{uuid4().hex}")
    await env.due(receipt.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == receipt.job_id
    await build.run_batch(lease)
    return fixture, receipt


async def published_state(env, index_id):
    async with env.factory() as db:
        row = await db.get(IndexVersion, index_id)
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        members = list(
            await db.scalars(
                select(IndexMember.chunk_id)
                .where(
                    IndexMember.index_version_id == index_id,
                    IndexMember.status == "written",
                )
                .order_by(IndexMember.ordinal)
            )
        )
    return row, corpus, members


def search_ids(adapter: MilvusIndex, repository_id, index_id):
    vector = [0.0] * DIMENSION
    vector[0] = 1.0
    result = adapter.search(
        "indexed chunk",
        vector,
        (IndexFilter(repository_id, index_id),),
        limit=30,
    )
    return (
        {hit.chunk_id for hit in result.bm25},
        {hit.chunk_id for hit in result.dense},
    )


def other_fingerprint() -> IndexFingerprint:
    original = fingerprint()
    return IndexFingerprint(
        embedding="bge-m3@upgraded",
        tokenizer=original.tokenizer,
        analyzer=original.analyzer,
        chunking=original.chunking,
        schema=original.schema,
    )


async def test_index_restart_and_restore_mismatch(knowledge_env):
    env = knowledge_env
    collection = f"devflow_flow_{uuid4().hex[:20]}"
    adapter, client = make_index(collection)
    restarted = None
    restarted_client = None
    try:
        fixture, receipt = await publish_small(env, adapter)
        row, corpus, members = await published_state(env, receipt.index_id)
        assert (row.status, row.queryable) == ("ready", True)
        assert corpus.active_index_version_id == row.id
        assert len(members) == len(fixture.chunk_ids) == 4
        assert set(members) == set(fixture.chunk_ids)

        before_bm25, before_dense = search_ids(adapter, env.repository.id, row.id)
        assert before_bm25 == set(members)
        assert before_dense == set(members)

        # A Worker restart is a fresh client and adapter with no in-process
        # cache of the previous build; ensure_schema re-loads the collection.
        restarted, restarted_client = make_index(collection)
        restarted.ensure_schema(fingerprint())
        assert restarted.verify_visible(row.id, members) is True
        after_bm25, after_dense = search_ids(restarted, env.repository.id, row.id)
        assert after_bm25 == before_bm25
        assert after_dense == before_dense

        availability = IndexAvailabilityService(env.factory, index=restarted)
        ready = await availability.check(
            env.repository.id,
            fingerprint=fingerprint(),
            verify_storage=True,
        )
        assert ready.status == "ready"
        assert ready.index_id == row.id
        ready.require()

        # Milvus vectors were reclaimed while the restored database still
        # points at the version: the bounded probe must say so explicitly.
        restarted.delete_version(row.id)
        reclaimed = await availability.check(
            env.repository.id,
            fingerprint=fingerprint(),
            verify_storage=True,
        )
        assert (reclaimed.status, reclaimed.reason) == (
            "stale",
            "vectors_missing",
        )
        with pytest.raises(AppError) as raised:
            reclaimed.require()
        assert raised.value.code == "index_not_ready"
        assert raised.value.status == 409

        # A database restored over a cleaned Milvus install has no collection
        # at all. A read-only probe must not recreate it or claim ready.
        client.drop_collection(collection)
        missing, missing_client = make_index(collection)
        try:
            absent = await IndexAvailabilityService(env.factory, index=missing).check(
                env.repository.id,
                fingerprint=fingerprint(),
                verify_storage=True,
            )
            assert (absent.status, absent.reason) == (
                "stale",
                "collection_missing",
            )
            with pytest.raises(AppError) as raised:
                absent.require()
            assert raised.value.code == "index_not_ready"
            # The read-only probe never creates a missing collection.
            assert missing.probe(fingerprint(), row.id, members) == "collection_missing"
        finally:
            close_index(missing_client, collection)

        # A different locally selected model/tokenizer revision invalidates
        # the published vectors even before any storage call.
        mismatched = await availability.check(
            env.repository.id, fingerprint=other_fingerprint()
        )
        assert (mismatched.status, mismatched.reason) == (
            "stale",
            "fingerprint_mismatch",
        )
        assert mismatched.index_id == row.id
    finally:
        if restarted_client is not None:
            close_index(restarted_client, collection)
        close_index(client, collection)


@pytest.mark.skipif(
    not RESTART_COMMAND,
    reason=(
        "TEST_MILVUS_RESTART_COMMAND is not set; Milvus server restart NOT EXECUTED"
    ),
)
async def test_milvus_server_restart_keeps_published_members(knowledge_env):
    env = knowledge_env
    collection = f"devflow_restart_{uuid4().hex[:20]}"
    adapter, client = make_index(collection)
    restarted = None
    restarted_client = None
    try:
        _fixture, receipt = await publish_small(env, adapter)
        row, _corpus, members = await published_state(env, receipt.index_id)
        before = search_ids(adapter, env.repository.id, row.id)

        client.close()
        # The command is operator-provided test infrastructure, never a
        # production credential or a shell string built from data.
        await asyncio.to_thread(
            subprocess.run,
            shlex.split(RESTART_COMMAND),
            check=True,
            timeout=120,
        )
        deadline = time.monotonic() + 120
        while not collection_present(collection):
            if time.monotonic() >= deadline:
                raise AssertionError("Milvus did not restart within 120s")
            await asyncio.sleep(1)

        restarted, restarted_client = make_index(collection)
        restarted.ensure_schema(fingerprint())
        assert restarted.verify_visible(row.id, members) is True
        after_bm25, after_dense = search_ids(restarted, env.repository.id, row.id)
        assert after_bm25 == before[0]
        assert after_dense == before[1]
        ready = await IndexAvailabilityService(env.factory, index=restarted).check(
            env.repository.id,
            fingerprint=fingerprint(),
            verify_storage=True,
        )
        assert ready.status == "ready"
        assert ready.index_id == row.id
    finally:
        if restarted_client is not None:
            close_index(restarted_client, collection)
        close_index(client, collection)
