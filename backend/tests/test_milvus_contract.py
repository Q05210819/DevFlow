"""Contract tests for the bounded, versioned Milvus adapter."""

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from devflow.errors import AppError
from devflow.knowledge.milvus import (
    ANALYZER,
    ANALYZER_VERSION,
    DIMENSION,
    MAX_FILTERS,
    MAX_SEARCH_LIMIT,
    MAX_UPSERT_ROWS,
    SCHEMA_VERSION,
    VERIFY_BATCH_SIZE,
    IndexFilter,
    IndexFingerprint,
    IndexRow,
    MilvusIndex,
)


def fingerprint(**overrides: str) -> IndexFingerprint:
    values = {
        "embedding": "bge-m3@revision",
        "tokenizer": "bge-m3-tokenizer-v1",
        "analyzer": ANALYZER_VERSION,
        "chunking": "chunking-v1",
        "schema": SCHEMA_VERSION,
    }
    values.update(overrides)
    return IndexFingerprint(**values)


def vector(hot: int = 0) -> list[float]:
    values = [0.0] * DIMENSION
    values[hot] = 1.0
    return values


def row(*, chunk: UUID | None = None, repo: UUID | None = None) -> IndexRow:
    return IndexRow(
        chunk_id=chunk or uuid4(),
        repository_id=repo or uuid4(),
        text="知识检索 embedding text",
        dense=vector(),
    )


def existing_description() -> dict[str, Any]:
    """Shape observed from a real Milvus 2.6 describe_collection response."""
    return {
        "collection_name": "devflow_knowledge_v1",
        "auto_id": False,
        "enable_dynamic_field": False,
        "consistency_level": 0,
        "consistency_level_name": "Strong",
        "fields": [
            {
                "name": "pk",
                "type": 21,
                "params": {"max_length": 64},
                "is_primary": True,
            },
            {
                "name": "repository_id",
                "type": 21,
                "params": {"max_length": 36},
            },
            {
                "name": "index_version_id",
                "type": 21,
                "params": {"max_length": 36},
            },
            {"name": "chunk_id", "type": 21, "params": {"max_length": 36}},
            {
                "name": "text",
                "type": 21,
                "params": {
                    "max_length": 8192,
                    "enable_analyzer": "true",
                    "analyzer_params": json.dumps(ANALYZER),
                },
            },
            {"name": "dense", "type": 101, "params": {"dim": 1024}},
            {
                "name": "sparse",
                "type": 104,
                "params": {},
                "is_function_output": True,
            },
        ],
        "functions": [
            {
                "name": "text_bm25",
                "type": 1,
                "input_field_names": "['text']",
                "output_field_names": "['sparse']",
            }
        ],
    }


class FakeSchema:
    def __init__(self, **options: object) -> None:
        self.options = options
        self.fields: list[dict[str, object]] = []
        self.functions: list[object] = []

    def add_field(self, name: str, dtype: object, **options: object) -> None:
        self.fields.append({"name": name, "type": int(dtype), **options})

    def add_function(self, function: object) -> None:
        self.functions.append(function)


class FakeIndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict[str, object]] = []

    def add_index(self, field_name: str, **options: object) -> None:
        self.indexes.append({"field_name": field_name, **options})


def existing_indexes() -> dict[str, dict[str, Any]]:
    """Shapes observed from real Milvus 2.6 describe_index responses."""
    return {
        "sparse": {
            "index_type": "SPARSE_INVERTED_INDEX",
            "metric_type": "BM25",
            "field_name": "sparse",
            "index_name": "sparse",
            "inverted_index_algo": "DAAT_MAXSCORE",
            "bm25_k1": "1.2",
            "bm25_b": "0.75",
            "state": "Finished",
        },
        "dense": {
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "field_name": "dense",
            "index_name": "dense",
            "M": "16",
            "efConstruction": "200",
            "state": "Finished",
        },
    }


class FakeClient:
    def __init__(
        self,
        *,
        exists: bool = False,
        description: dict[str, Any] | None = None,
        index_descriptions: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.exists = exists
        self.description = description or existing_description()
        self.index_descriptions = index_descriptions or existing_indexes()
        self.described_indexes: list[tuple[str, str, float | None]] = []
        self.count_handler: Callable[[str], int] | None = None
        self.indexes = ["sparse", "dense"] if exists else []
        self.schema: FakeSchema | None = None
        self.created_schema: FakeSchema | None = None
        self.created_index_params: FakeIndexParams | None = None
        self.created_consistency: str | None = None
        self.created_timeout: float | None = None
        self.created = 0
        self.loaded: list[tuple[str, float | None]] = []
        self.upserts: list[tuple[str, list[dict[str, object]], float | None]] = []
        self.queries: list[dict[str, object]] = []
        self.searches: list[dict[str, object]] = []
        self.deletes: list[tuple[str, str, float | None]] = []
        self.query_handler: Callable[[str, int], list[dict[str, object]]] | None = None
        self.search_results: dict[str, list[list[dict[str, object]]]] = {
            "sparse": [[]],
            "dense": [[]],
        }
        self.fail_on: set[str] = set()

    def _check(self, operation: str) -> None:
        if operation in self.fail_on:
            raise RuntimeError(f"{operation} unavailable")

    def has_collection(self, name: str, timeout: float | None = None) -> bool:
        self._check("has_collection")
        return self.exists

    def create_schema(self, **options: object) -> FakeSchema:
        self.schema = FakeSchema(**options)
        return self.schema

    def prepare_index_params(self) -> FakeIndexParams:
        return FakeIndexParams()

    def create_collection(
        self,
        name: str,
        *,
        schema: FakeSchema,
        index_params: FakeIndexParams,
        consistency_level: str,
        timeout: float,
    ) -> None:
        self._check("create_collection")
        self.exists = True
        self.created += 1
        self.created_schema = schema
        self.created_index_params = index_params
        self.created_consistency = consistency_level
        self.created_timeout = timeout
        self.indexes = [str(item["field_name"]) for item in index_params.indexes]

    def describe_collection(
        self, name: str, timeout: float | None = None
    ) -> dict[str, Any]:
        self._check("describe_collection")
        return self.description

    def load_collection(self, name: str, timeout: float | None = None) -> None:
        self._check("load_collection")
        self.loaded.append((name, timeout))

    def list_indexes(self, name: str) -> list[str]:
        self._check("list_indexes")
        return list(self.indexes)

    def describe_index(
        self, name: str, index_name: str, timeout: float | None = None
    ) -> dict[str, Any]:
        self._check("describe_index")
        self.described_indexes.append((name, index_name, timeout))
        return dict(self.index_descriptions.get(index_name, {}))

    def upsert(
        self,
        name: str,
        data: list[dict[str, object]],
        timeout: float | None = None,
    ) -> None:
        self._check("upsert")
        self.upserts.append((name, data, timeout))

    def query(
        self,
        name: str,
        *,
        filter: str,
        output_fields: list[str],
        consistency_level: str,
        limit: int | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, object]]:
        self._check("query")
        self.queries.append(
            {
                "name": name,
                "filter": filter,
                "output_fields": output_fields,
                "consistency_level": consistency_level,
                "limit": limit,
                "timeout": timeout,
            }
        )
        if output_fields == ["count(*)"]:
            value = 0 if self.count_handler is None else self.count_handler(filter)
            return [{"count(*)": value}]
        if self.query_handler is None:
            return []
        return self.query_handler(filter, limit)

    def search(self, name: str, **options: object) -> list[list[dict[str, object]]]:
        self._check("search")
        self.searches.append({"name": name, **options})
        return self.search_results[str(options["anns_field"])]

    def delete(self, name: str, *, filter: str, timeout: float | None = None) -> None:
        self._check("delete")
        self.deletes.append((name, filter, timeout))


def ready_index(settings_factory, client: FakeClient) -> MilvusIndex:
    index = MilvusIndex(settings_factory(), client=client)
    index.ensure_schema(fingerprint())
    return index


def assert_unavailable(error: AppError) -> None:
    assert error.code == "service_unavailable"
    assert error.status == 503


def test_ensure_schema_creates_fixed_versioned_schema(settings_factory):
    client = FakeClient(exists=False)
    index = MilvusIndex(settings_factory(), client=client)
    index.ensure_schema(fingerprint())

    assert client.created == 1
    assert client.created_timeout == 120
    assert client.created_consistency == "Strong"
    assert client.loaded == [("devflow_knowledge_v1", 120.0)]

    assert client.created_schema is not None
    assert client.created_schema.options == {
        "auto_id": False,
        "enable_dynamic_field": False,
    }
    fields = {field["name"]: field for field in client.created_schema.fields}
    assert set(fields) == {
        "pk",
        "repository_id",
        "index_version_id",
        "chunk_id",
        "text",
        "dense",
        "sparse",
    }
    assert fields["pk"]["type"] == 21
    assert fields["pk"]["is_primary"] is True
    assert fields["pk"]["max_length"] == 64
    for name in ("repository_id", "index_version_id", "chunk_id"):
        assert fields[name]["type"] == 21
        assert fields[name]["max_length"] == 36
    assert fields["text"]["type"] == 21
    assert fields["text"]["max_length"] == 8192
    assert fields["text"]["enable_analyzer"] is True
    assert fields["text"]["analyzer_params"] == {"type": "chinese"}
    assert fields["dense"]["type"] == 101
    assert fields["dense"]["dim"] == DIMENSION
    assert fields["sparse"]["type"] == 104

    functions = client.created_schema.functions
    assert len(functions) == 1
    function = functions[0].to_dict()
    assert function["type"] == 1
    assert function["input_field_names"] == ["text"]
    assert function["output_field_names"] == ["sparse"]

    assert client.created_index_params is not None
    indexes = {item["field_name"]: item for item in client.created_index_params.indexes}
    assert indexes["sparse"]["index_type"] == "SPARSE_INVERTED_INDEX"
    assert indexes["sparse"]["metric_type"] == "BM25"
    assert indexes["sparse"]["params"] == {
        "inverted_index_algo": "DAAT_MAXSCORE",
        "bm25_k1": 1.2,
        "bm25_b": 0.75,
    }
    assert indexes["dense"]["index_type"] == "HNSW"
    assert indexes["dense"]["metric_type"] == "COSINE"
    assert indexes["dense"]["params"] == {"M": 16, "efConstruction": 200}


def test_ensure_schema_reuses_and_validates_existing_collection(settings_factory):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)
    index.ensure_schema(fingerprint())
    index.ensure_schema(fingerprint())

    assert client.created == 0
    assert client.loaded == [("devflow_knowledge_v1", 120.0)]


class RepeatedNames:
    """A non-list iterable, like PyMilvus' protobuf repeated containers."""

    def __init__(self, values: list[str]) -> None:
        self.values = values

    def __iter__(self):
        return iter(self.values)


def test_ensure_schema_accepts_protobuf_repeated_function_fields(settings_factory):
    description = existing_description()
    function = description["functions"][0]
    function["input_field_names"] = RepeatedNames(["text"])
    function["output_field_names"] = RepeatedNames(["sparse"])
    client = FakeClient(exists=True, description=description)

    MilvusIndex(settings_factory(), client=client).ensure_schema(fingerprint())

    assert client.created == 0
    assert client.loaded == [("devflow_knowledge_v1", 120.0)]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["fields"][5]["params"].update(dim=768),
        lambda value: value["fields"][4]["params"].update(
            analyzer_params='{"type":"standard"}'
        ),
        lambda value: value["fields"][4]["params"].update(enable_analyzer="false"),
        lambda value: value["functions"].clear(),
        lambda value: value["fields"].pop(),
        lambda value: value["fields"][0]["params"].update(max_length=32),
        lambda value: value.update(auto_id=True),
        lambda value: value.update(enable_dynamic_field=True),
        lambda value: value.update(consistency_level_name="Bounded"),
        lambda value: value.pop("consistency_level_name"),
        lambda value: value["fields"][0].update(is_primary=False),
    ],
)
def test_ensure_schema_rejects_foreign_collection(settings_factory, mutate):
    description = existing_description()
    mutate(description)
    client = FakeClient(exists=True, description=description)
    index = MilvusIndex(settings_factory(), client=client)

    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint())
    assert_unavailable(caught.value)
    assert client.loaded == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["dense"].update(metric_type="L2"),
        lambda value: value["sparse"].update(metric_type="IP"),
        lambda value: value["dense"].update(index_type="IVF_FLAT"),
        lambda value: value["sparse"].update(inverted_index_algo="TAIL"),
        lambda value: value["dense"].update(M="32"),
        lambda value: value["sparse"].update(bm25_k1="2.0"),
        lambda value: value["dense"].update(field_name="text"),
        lambda value: value["sparse"].update(index_name="other"),
    ],
)
def test_ensure_schema_rejects_foreign_indexes(settings_factory, mutate):
    indexes = existing_indexes()
    mutate(indexes)
    client = FakeClient(exists=True, index_descriptions=indexes)
    index = MilvusIndex(settings_factory(), client=client)

    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint())
    assert_unavailable(caught.value)
    assert client.loaded == []


def test_ensure_schema_rejects_extra_index_names(settings_factory):
    client = FakeClient(exists=True)
    client.indexes.append("unexpected")
    index = MilvusIndex(settings_factory(), client=client)

    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint())
    assert_unavailable(caught.value)
    assert client.loaded == []


def test_ensure_schema_requires_matching_versions(settings_factory):
    client = FakeClient(exists=False)
    index = MilvusIndex(settings_factory(), client=client)
    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint(schema="other-schema"))
    assert_unavailable(caught.value)
    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint(analyzer="other-analyzer"))
    assert_unavailable(caught.value)
    assert client.created == 0


def test_ensure_schema_wraps_connection_failure(settings_factory):
    client = FakeClient(exists=False)
    client.fail_on.add("has_collection")
    index = MilvusIndex(settings_factory(), client=client)
    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint())
    assert_unavailable(caught.value)


def test_load_failure_never_marks_schema_ready(settings_factory):
    client = FakeClient(exists=False)
    client.fail_on.add("load_collection")
    index = MilvusIndex(settings_factory(), client=client)
    with pytest.raises(AppError) as caught:
        index.ensure_schema(fingerprint())
    assert_unavailable(caught.value)
    client.fail_on.clear()
    with pytest.raises(AppError) as caught:
        index.verify_visible(uuid4(), [uuid4()])
    assert_unavailable(caught.value)


def test_operations_require_ready_schema(settings_factory):
    index = MilvusIndex(settings_factory(), client=FakeClient(exists=False))
    with pytest.raises(AppError) as caught:
        index.upsert(uuid4(), [row()])
    assert_unavailable(caught.value)
    with pytest.raises(AppError) as caught:
        index.verify_visible(uuid4(), [uuid4()])
    assert_unavailable(caught.value)
    with pytest.raises(AppError) as caught:
        index.search("query", vector(), (IndexFilter(uuid4(), uuid4()),))
    assert_unavailable(caught.value)
    with pytest.raises(AppError) as caught:
        index.delete_version(uuid4())
    assert_unavailable(caught.value)


def test_upsert_uses_stable_keys_and_bounded_payload(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    version = uuid4()
    chunk = uuid4()
    repository = uuid4()
    row_value = IndexRow(
        chunk_id=chunk,
        repository_id=repository,
        text="src/devflow/knowledge/milvus.py",
        dense=vector(3),
    )

    index.upsert(version, [row_value])
    index.upsert(version, [row_value])

    assert len(client.upserts) == 2
    name, data, timeout = client.upserts[0]
    assert name == "devflow_knowledge_v1"
    assert timeout == 120
    assert len(data) == 1
    record = data[0]
    assert record["pk"] == hashlib.sha256(f"{version}:{chunk}".encode()).hexdigest()
    assert record["repository_id"] == str(repository)
    assert record["index_version_id"] == str(version)
    assert record["chunk_id"] == str(chunk)
    assert record["text"] == "src/devflow/knowledge/milvus.py"
    assert record["dense"] == vector(3)
    assert client.upserts[1][1][0]["pk"] == record["pk"]


def test_upsert_deduplicates_identical_rows_and_rejects_conflicts(
    settings_factory,
):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    version = uuid4()
    chunk = uuid4()
    repository = uuid4()
    first = IndexRow(chunk, repository, "one", vector(0))
    same = IndexRow(chunk, repository, "one", vector(0))
    different = IndexRow(chunk, repository, "two", vector(0))

    index.upsert(version, [first, same])
    assert len(client.upserts[0][1]) == 1

    with pytest.raises(ValueError):
        index.upsert(version, [first, different])


def test_upsert_bounds_rows_and_validates_values(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)

    with pytest.raises(ValueError):
        index.upsert(uuid4(), [row()] * (MAX_UPSERT_ROWS + 1))
    with pytest.raises(TypeError):
        index.upsert(uuid4(), ["not-a-row"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        IndexRow(uuid4(), uuid4(), "text", [0.0] * DIMENSION)
    with pytest.raises(ValueError):
        IndexRow(uuid4(), uuid4(), "text", [1.0] * (DIMENSION - 1))
    with pytest.raises(ValueError):
        IndexRow(uuid4(), uuid4(), "text", [float("nan")] * DIMENSION)
    with pytest.raises(TypeError):
        IndexRow(uuid4(), uuid4(), "text", "dense")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        IndexRow(uuid4(), uuid4(), "", vector())
    with pytest.raises(ValueError):
        IndexRow(uuid4(), uuid4(), "x" * 8193, vector())
    with pytest.raises(TypeError):
        IndexRow("not-a-uuid", uuid4(), "text", vector())  # type: ignore[arg-type]
    assert client.upserts == []


def test_upsert_wraps_client_failure(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    client.fail_on.add("upsert")
    with pytest.raises(AppError) as caught:
        index.upsert(uuid4(), [row()])
    assert_unavailable(caught.value)


def test_probe_reports_missing_collection_without_creating_or_loading(
    settings_factory,
):
    client = FakeClient(exists=False)
    index = MilvusIndex(settings_factory(), client=client)

    assert index.probe(fingerprint(), uuid4(), [uuid4()]) == "collection_missing"
    assert client.created == 0
    assert client.loaded == []
    assert client.queries == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["fields"][5]["params"].update(dim=768),
        lambda value: value["fields"][4]["params"].update(
            analyzer_params='{"type":"standard"}'
        ),
    ],
)
def test_probe_reports_schema_mismatch(settings_factory, mutate):
    description = existing_description()
    mutate(description)
    client = FakeClient(exists=True, description=description)
    index = MilvusIndex(settings_factory(), client=client)

    assert index.probe(fingerprint(), uuid4(), [uuid4()]) == "schema_mismatch"
    assert client.loaded == []
    assert client.queries == []


def test_probe_rejects_unsupported_fingerprint_without_contacting_milvus(
    settings_factory,
):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)

    assert index.probe(fingerprint(schema="other"), uuid4(), [uuid4()]) == (
        "schema_unsupported"
    )
    assert index.probe(fingerprint(analyzer="other"), uuid4(), [uuid4()]) == (
        "schema_unsupported"
    )
    assert client.queries == []


def test_probe_reports_missing_vectors_and_success(settings_factory):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)
    version = uuid4()
    chunk = uuid4()

    assert index.probe(fingerprint(), version, [chunk]) == "vectors_missing"
    assert len(client.queries) == 1
    assert client.queries[0]["consistency_level"] == "Strong"

    client.queries.clear()
    client.query_handler = lambda expression, limit: [
        {"chunk_id": str(chunk), "index_version_id": str(version)}
    ]
    assert index.probe(fingerprint(), version, [chunk]) is None
    assert len(client.queries) == 1


def test_probe_checks_exact_version_count_before_sampling(settings_factory):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)
    version = uuid4()
    chunk = uuid4()
    client.query_handler = lambda expression, limit: [
        {"chunk_id": str(chunk), "index_version_id": str(version)}
    ]
    client.count_handler = lambda expression: 3

    # A vector outside the bounded ID sample was lost: the exact count still
    # detects it, and the sample is not needed.
    assert (
        index.probe(fingerprint(), version, [chunk], expected_count=4)
        == "vectors_missing"
    )
    counts = [item for item in client.queries if item["output_fields"] == ["count(*)"]]
    assert len(counts) == 1
    assert counts[0]["consistency_level"] == "Strong"
    assert "index_version_id" in str(counts[0]["filter"])
    assert len(client.queries) == 1

    client.queries.clear()
    assert index.probe(fingerprint(), version, [chunk], expected_count=3) is None
    assert [item["output_fields"] for item in client.queries] == [
        ["count(*)"],
        ["chunk_id", "index_version_id"],
    ]


def test_probe_verifies_zero_member_manifests_against_collection(settings_factory):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)
    version = uuid4()

    assert index.probe(fingerprint(), version, [], expected_count=0) is None
    assert client.queries[0]["output_fields"] == ["count(*)"]

    client.queries.clear()
    client.count_handler = lambda expression: 1
    assert index.probe(fingerprint(), version, [], expected_count=0) == (
        "vectors_missing"
    )
    assert [item["output_fields"] for item in client.queries] == [["count(*)"]]


@pytest.mark.parametrize("value", [-1, 1.5, True, "2"])
def test_probe_rejects_invalid_expected_count(settings_factory, value):
    index = MilvusIndex(settings_factory(), client=FakeClient(exists=True))
    with pytest.raises(TypeError):
        index.probe(fingerprint(), uuid4(), [], expected_count=value)


def test_probe_rejects_malformed_count_response(settings_factory):
    client = FakeClient(exists=True)
    client.count_handler = lambda expression: "3"
    index = MilvusIndex(settings_factory(), client=client)
    with pytest.raises(AppError) as caught:
        index.probe(fingerprint(), uuid4(), [], expected_count=3)
    assert_unavailable(caught.value)


def test_probe_wraps_index_metadata_transport_failure(settings_factory):
    client = FakeClient(exists=True)
    client.fail_on.add("describe_index")
    index = MilvusIndex(settings_factory(), client=client)
    with pytest.raises(AppError) as caught:
        index.probe(fingerprint(), uuid4(), [uuid4()])
    assert_unavailable(caught.value)


def test_probe_raises_on_transport_failure(settings_factory):
    client = FakeClient(exists=True)
    client.fail_on.add("has_collection")
    index = MilvusIndex(settings_factory(), client=client)
    with pytest.raises(AppError) as caught:
        index.probe(fingerprint(), uuid4(), [uuid4()])
    assert_unavailable(caught.value)


def test_verify_visible_batches_exact_ids_with_strong_consistency(
    settings_factory,
):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    version = uuid4()
    chunks = [uuid4() for _ in range(VERIFY_BATCH_SIZE + 4)]
    client.query_handler = lambda expression, limit: [
        {
            "chunk_id": str(chunk),
            "index_version_id": str(version),
        }
        for chunk in chunks
        if str(chunk) in expression
    ][:limit]

    assert index.verify_visible(version, chunks) is True
    assert len(client.queries) == 2
    assert client.queries[0]["limit"] == VERIFY_BATCH_SIZE
    assert client.queries[1]["limit"] == 4
    for query in client.queries:
        assert query["consistency_level"] == "Strong"
        assert query["output_fields"] == ["chunk_id", "index_version_id"]
        assert query["timeout"] == 120
        assert str(version) in str(query["filter"])
        assert json.dumps(str(version)) in str(query["filter"])
        for chunk in chunks:
            if str(chunk) in str(query["filter"]):
                assert json.dumps(str(chunk)) in str(query["filter"])


def test_verify_visible_handles_empty_duplicates_and_missing(
    settings_factory,
):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    version = uuid4()
    chunk = uuid4()

    assert index.verify_visible(version, []) is True
    assert client.queries == []

    client.query_handler = lambda expression, limit: []
    assert index.verify_visible(version, [chunk, chunk]) is False
    assert len(client.queries) == 1

    client.queries.clear()
    client.query_handler = lambda expression, limit: [
        {"chunk_id": str(chunk), "index_version_id": str(version)}
    ]
    assert index.verify_visible(version, [chunk]) is True


@pytest.mark.parametrize("mode", ["foreign_chunk", "foreign_version"])
def test_verify_visible_rejects_unexpected_rows(settings_factory, mode):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    version = uuid4()
    chunk = uuid4()
    observed = uuid4() if mode == "foreign_chunk" else chunk
    observed_version = uuid4() if mode == "foreign_version" else version
    client.query_handler = lambda expression, limit: [
        {
            "chunk_id": str(observed),
            "index_version_id": str(observed_version),
        }
    ]

    with pytest.raises(AppError) as caught:
        index.verify_visible(version, [chunk])
    assert_unavailable(caught.value)


def test_verify_visible_wraps_client_failure(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    client.fail_on.add("query")
    with pytest.raises(AppError) as caught:
        index.verify_visible(uuid4(), [uuid4()])
    assert_unavailable(caught.value)


def test_search_runs_bm25_and_dense_with_identical_server_scope(
    settings_factory,
):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    repository = uuid4()
    version = uuid4()
    chunk = uuid4()
    query_text = 'alpha" or repository_id != "anything'
    client.search_results = {
        "sparse": [
            [
                {
                    "distance": 7.5,
                    "entity": {
                        "repository_id": str(repository),
                        "index_version_id": str(version),
                        "chunk_id": str(chunk),
                    },
                }
            ]
        ],
        "dense": [
            [
                {
                    "distance": 0.875,
                    "entity": {
                        "repository_id": str(repository),
                        "index_version_id": str(version),
                        "chunk_id": str(chunk),
                    },
                }
            ]
        ],
    }
    filters = (IndexFilter(repository, version),)

    result = index.search(query_text, vector(0), filters, limit=12)

    assert len(client.searches) == 2
    bm25, dense = client.searches
    assert bm25["filter"] == dense["filter"]
    expression = str(bm25["filter"])
    assert json.dumps(str(repository)) in expression
    assert json.dumps(str(version)) in expression
    assert query_text not in expression
    assert "anything" not in expression
    assert bm25["anns_field"] == "sparse"
    assert bm25["data"] == [query_text]
    assert bm25["search_params"] == {"metric_type": "BM25"}
    assert dense["anns_field"] == "dense"
    assert dense["data"] == [vector(0)]
    assert dense["search_params"]["metric_type"] == "COSINE"
    assert dense["search_params"]["params"]["ef"] >= 64
    for search in (bm25, dense):
        assert search["limit"] == 12
        assert search["consistency_level"] == "Strong"
        assert search["timeout"] == 5.0
        assert search["output_fields"] == [
            "repository_id",
            "chunk_id",
            "index_version_id",
        ]

    assert result.bm25[0].chunk_id == chunk
    assert result.bm25[0].index_version_id == version
    assert result.bm25[0].rank == 1
    assert result.bm25[0].score == 7.5
    assert result.dense[0].score == 0.875


def test_search_builds_one_expression_for_all_filters(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    pairs = [(uuid4(), uuid4()) for _ in range(MAX_FILTERS)]
    filters = tuple(IndexFilter(repo, version) for repo, version in pairs)
    client.search_results = {"sparse": [[]], "dense": [[]]}

    index.search("query", vector(), filters)

    expressions = {str(item["filter"]) for item in client.searches}
    assert len(expressions) == 1
    expression = expressions.pop()
    assert expression.count(" or ") == MAX_FILTERS - 1
    for repository, version in pairs:
        assert json.dumps(str(repository)) in expression
        assert json.dumps(str(version)) in expression


def test_search_deduplicates_ranked_hits(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    repository = uuid4()
    version = uuid4()
    first, second = uuid4(), uuid4()

    def hit(chunk: UUID, score: float) -> dict[str, object]:
        return {
            "distance": score,
            "entity": {
                "repository_id": str(repository),
                "index_version_id": str(version),
                "chunk_id": str(chunk),
            },
        }

    client.search_results = {
        "sparse": [[hit(first, 3.0), hit(first, 2.0), hit(second, 1.0)]],
        "dense": [[hit(second, 0.9), hit(first, 0.5)]],
    }

    result = index.search("query", vector(), (IndexFilter(repository, version),))
    assert [item.chunk_id for item in result.bm25] == [first, second]
    assert [item.rank for item in result.bm25] == [1, 2]
    assert [item.chunk_id for item in result.dense] == [second, first]
    assert [item.rank for item in result.dense] == [1, 2]


def test_search_rejects_out_of_scope_results(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    repository = uuid4()
    version = uuid4()
    client.search_results = {
        "sparse": [
            [
                {
                    "distance": 1.0,
                    "entity": {
                        "repository_id": str(uuid4()),
                        "index_version_id": str(version),
                        "chunk_id": str(uuid4()),
                    },
                }
            ]
        ],
        "dense": [[]],
    }

    with pytest.raises(AppError) as caught:
        index.search("query", vector(), (IndexFilter(repository, version),))
    assert_unavailable(caught.value)


@pytest.mark.parametrize(
    "case",
    [
        "empty_text",
        "long_text",
        "zero_vector",
        "short_vector",
        "no_filters",
        "too_many_filters",
        "zero_limit",
        "large_limit",
        "bool_limit",
        "not_a_filter",
        "not_a_tuple",
    ],
)
def test_search_rejects_unbounded_parameters(settings_factory, case):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    repository = uuid4()
    version = uuid4()
    filters: object = (IndexFilter(repository, version),)
    query_text = "query"
    query_vector = vector()
    limit = 30

    if case == "empty_text":
        query_text = "  "
    elif case == "long_text":
        query_text = "x" * 8193
    elif case == "zero_vector":
        query_vector = [0.0] * DIMENSION
    elif case == "short_vector":
        query_vector = [1.0]
    elif case == "no_filters":
        filters = ()
    elif case == "too_many_filters":
        filters = tuple(IndexFilter(uuid4(), uuid4()) for _ in range(11))
    elif case == "zero_limit":
        limit = 0
    elif case == "large_limit":
        limit = MAX_SEARCH_LIMIT + 1
    elif case == "bool_limit":
        limit = True
    elif case == "not_a_filter":
        filters = ("bad",)
    elif case == "not_a_tuple":
        filters = [IndexFilter(repository, version)]

    with pytest.raises((TypeError, ValueError)):
        index.search(
            query_text,
            query_vector,
            filters,  # type: ignore[arg-type]
            limit=limit,  # type: ignore[arg-type]
        )
    assert client.searches == []


def test_search_wraps_client_failure(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    client.fail_on.add("search")
    with pytest.raises(AppError) as caught:
        index.search("query", vector(), (IndexFilter(uuid4(), uuid4()),))
    assert_unavailable(caught.value)


def test_delete_version_uses_exact_scope(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    version = uuid4()

    index.delete_version(version)

    assert len(client.deletes) == 1
    name, expression, timeout = client.deletes[0]
    assert name == "devflow_knowledge_v1"
    assert expression == f"index_version_id == {json.dumps(str(version))}"
    assert timeout == 120


def test_delete_version_wraps_client_failure(settings_factory):
    client = FakeClient()
    index = ready_index(settings_factory, client)
    client.fail_on.add("delete")
    with pytest.raises(AppError) as caught:
        index.delete_version(uuid4())
    assert_unavailable(caught.value)


def test_purge_version_deletes_exact_scope_without_loading(settings_factory):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)
    version = uuid4()

    assert index.purge_version(version) is True

    assert client.loaded == []
    assert client.deletes == [
        (
            "devflow_knowledge_v1",
            f"index_version_id == {json.dumps(str(version))}",
            120,
        )
    ]


def test_purge_version_reports_absent_collection_without_creating_it(
    settings_factory,
):
    client = FakeClient(exists=False)
    index = MilvusIndex(settings_factory(), client=client)

    assert index.purge_version(uuid4()) is False
    assert client.created == 0
    assert client.deletes == []


def test_purge_version_wraps_delete_failure(settings_factory):
    client = FakeClient(exists=True)
    index = MilvusIndex(settings_factory(), client=client)
    client.fail_on.add("delete")
    with pytest.raises(AppError) as caught:
        index.purge_version(uuid4())
    assert_unavailable(caught.value)


def test_milvus_settings_accept_http_origin(settings_factory):
    settings = settings_factory(
        milvus_uri="http://milvus.internal:19530/",
        milvus_collection="custom_knowledge_1",
        milvus_token="local-token",
        milvus_timeout_seconds=3,
        milvus_build_timeout_seconds=300,
    )
    assert settings.milvus_uri == "http://milvus.internal:19530"
    assert settings.milvus_collection == "custom_knowledge_1"
    assert settings.milvus_token.get_secret_value() == "local-token"
    assert settings.milvus_timeout_seconds == 3
    assert settings.milvus_build_timeout_seconds == 300
    assert "local-token" not in repr(settings)


@pytest.mark.parametrize(
    "value",
    [
        "grpc://milvus:19530",
        "http://user:pass@milvus:19530",
        "http://milvus:19530/db",
        "http://milvus:19530?token=secret",
        "http://milvus:19530#fragment",
        "milvus:19530",
        "http://",
    ],
)
def test_milvus_settings_reject_non_origin_uri(settings_factory, value):
    with pytest.raises(ValidationError):
        settings_factory(milvus_uri=value)


@pytest.mark.parametrize(
    "value", ["", "9collection", "bad-name", "bad name", "x" * 256]
)
def test_milvus_settings_reject_invalid_collection(settings_factory, value):
    with pytest.raises(ValidationError):
        settings_factory(milvus_collection=value)


@pytest.mark.parametrize("value", [0, -1, 5.5, 6])
def test_milvus_settings_bound_timeouts(settings_factory, value):
    with pytest.raises(ValidationError):
        settings_factory(milvus_timeout_seconds=value)
    if value not in (5.5, 6):
        with pytest.raises(ValidationError):
            settings_factory(milvus_build_timeout_seconds=value)


def test_public_types_require_uuids():
    with pytest.raises(TypeError):
        IndexFilter("repo", uuid4())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        IndexFingerprint("", "tokenizer", ANALYZER_VERSION, "chunking", SCHEMA_VERSION)
