"""Real Milvus integration tests for the versioned knowledge index.

These tests run only when ``TEST_MILVUS_URI`` points at an isolated test
service. Without it they are reported as skipped/NOT EXECUTED rather than
silently passing against a mock.
"""

import os
from uuid import uuid4

import pytest

from devflow.config import Settings
from devflow.knowledge.milvus import (
    ANALYZER_VERSION,
    DIMENSION,
    SCHEMA_VERSION,
    IndexFilter,
    IndexFingerprint,
    IndexRow,
    MilvusIndex,
)

MILVUS_URI = os.environ.get("TEST_MILVUS_URI")

pytestmark = pytest.mark.skipif(
    not MILVUS_URI,
    reason="TEST_MILVUS_URI is not set; real Milvus integration NOT EXECUTED",
)


def fingerprint() -> IndexFingerprint:
    return IndexFingerprint(
        embedding="bge-m3@integration",
        tokenizer="bge-m3-tokenizer-v1",
        analyzer=ANALYZER_VERSION,
        chunking="chunking-v1",
        schema=SCHEMA_VERSION,
    )


def vector(hot: int) -> list[float]:
    values = [0.0] * DIMENSION
    values[hot % DIMENSION] = 1.0
    return values


def make_row(repository, text: str, hot: int) -> IndexRow:
    return IndexRow(
        chunk_id=uuid4(),
        repository_id=repository,
        text=text,
        dense=vector(hot),
    )


def in_scope(result, versions: set):
    for hits in (result.bm25, result.dense):
        for hit in hits:
            assert hit.index_version_id in versions


@pytest.fixture(scope="module")
def index():
    if not MILVUS_URI:
        pytest.skip("TEST_MILVUS_URI is not set; NOT EXECUTED")
    from pymilvus import MilvusClient

    collection = f"devflow_it_{uuid4().hex[:20]}"
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
    adapter = MilvusIndex(settings, client=client)
    adapter.ensure_schema(fingerprint())
    try:
        yield adapter
    finally:
        try:
            client.drop_collection(collection)
        finally:
            client.close()


def test_strong_visibility_stable_keys_and_version_delete(index):
    repository = uuid4()
    version = uuid4()
    rows = [
        make_row(repository, f"versioned chunk {number} alpha", number)
        for number in range(20)
    ]
    chunk_ids = [row.chunk_id for row in rows]

    index.upsert(version, rows)
    assert index.verify_visible(version, chunk_ids) is True
    assert index.verify_visible(version, [uuid4()]) is False

    # Re-upserting the same chunk IDs replaces rows through the stable key.
    index.upsert(version, rows)
    assert index.verify_visible(version, chunk_ids) is True

    result = index.search(
        "versioned chunk alpha",
        vector(0),
        (IndexFilter(repository, version),),
        limit=30,
    )
    assert result.bm25
    assert result.dense
    in_scope(result, {version})
    assert len({hit.chunk_id for hit in result.bm25}) == len(result.bm25)
    assert [hit.rank for hit in result.bm25] == list(range(1, len(result.bm25) + 1))

    index.delete_version(version)
    assert index.verify_visible(version, chunk_ids[:1]) is False
    after = index.search(
        "versioned chunk alpha",
        vector(0),
        (IndexFilter(repository, version),),
        limit=30,
    )
    assert after.bm25 == ()
    assert after.dense == ()


def test_bm25_and_dense_share_exact_repository_and_version_scope(index):
    repository_a = uuid4()
    repository_b = uuid4()
    version_a1 = uuid4()
    version_a2 = uuid4()
    version_b = uuid4()
    text = "scopeunique marker for isolation"
    rows = {
        "a1": make_row(repository_a, text, 1),
        "a2": make_row(repository_a, text, 1),
        "b": make_row(repository_b, text, 1),
    }

    index.upsert(version_a1, [rows["a1"]])
    index.upsert(version_a2, [rows["a2"]])
    index.upsert(version_b, [rows["b"]])

    single = index.search(
        "scopeunique",
        vector(1),
        (IndexFilter(repository_a, version_a1),),
        limit=10,
    )
    assert [hit.chunk_id for hit in single.bm25] == [rows["a1"].chunk_id]
    assert [hit.chunk_id for hit in single.dense] == [rows["a1"].chunk_id]

    both = index.search(
        "scopeunique",
        vector(1),
        (
            IndexFilter(repository_a, version_a1),
            IndexFilter(repository_b, version_b),
        ),
        limit=10,
    )
    expected = {rows["a1"].chunk_id, rows["b"].chunk_id}
    assert {hit.chunk_id for hit in both.bm25} == expected
    assert {hit.chunk_id for hit in both.dense} == expected
    in_scope(both, {version_a1, version_b})

    # Query text is data, never filter syntax. A quote/expression payload
    # must not widen the server-side scope.
    injected = index.search(
        'scopeunique" or index_version_id != "',
        vector(1),
        (IndexFilter(repository_a, version_a2),),
        limit=10,
    )
    in_scope(injected, {version_a2})
    if injected.dense:
        assert {hit.chunk_id for hit in injected.dense} == {rows["a2"].chunk_id}

    index.delete_version(version_a1)
    assert index.verify_visible(version_a1, [rows["a1"].chunk_id]) is False
    assert index.verify_visible(version_a2, [rows["a2"].chunk_id]) is True
    assert index.verify_visible(version_b, [rows["b"].chunk_id]) is True


def test_chinese_english_paths_and_identifiers_are_tokenized(index):
    repository = uuid4()
    version = uuid4()
    cases = [
        ("知识检索服务", "知识检索"),
        ("information retrieval pipeline", "information retrieval"),
        ("src/devflow/knowledge/milvus.py", "src/devflow/knowledge/milvus.py"),
        ("snake_case_identifier", "snake_case_identifier"),
        ("CamelCaseIdentifier", "CamelCaseIdentifier"),
        ("ERR_QUOTA_EXCEEDED", "ERR_QUOTA_EXCEEDED"),
    ]
    rows = [
        make_row(repository, text, number) for number, (text, _) in enumerate(cases)
    ]
    index.upsert(version, rows)

    for number, (_, query) in enumerate(cases):
        result = index.search(
            query,
            vector(number),
            (IndexFilter(repository, version),),
            limit=10,
        )
        assert result.bm25, query
        assert result.bm25[0].chunk_id == rows[number].chunk_id, query
        assert result.bm25[0].index_version_id == version
        in_scope(result, {version})
