"""Bounded, versioned Milvus storage for knowledge chunks.

The collection keeps one deterministic primary key per (index version, chunk).
Dense search uses a fixed 1024-dimensional COSINE vector, while lexical search
uses Milvus' native BM25 function over the analyzed ``text`` field. Both routes
receive exactly the same server-side repository/index filters. User query text
is never interpolated into a filter expression.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from devflow.config import Settings
from devflow.errors import AppError

DIMENSION = 1024
SCHEMA_VERSION = "devflow-milvus-schema-v1"
ANALYZER = {"type": "chinese"}
ANALYZER_VERSION = "milvus-chinese-jieba-cnalphanumonly-v1"
MAX_TEXT_BYTES = 8192
MAX_UPSERT_ROWS = 256
MAX_SEARCH_LIMIT = 60
MAX_FILTERS = 10
VERIFY_BATCH_SIZE = 16
SEARCH_TIMEOUT_SECONDS = 5.0
_FINGERPRINT_BYTES = 256
INDEX_CONTRACT = {
    "dense": {
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "M": 16.0,
        "efConstruction": 200.0,
    },
    "sparse": {
        "index_type": "SPARSE_INVERTED_INDEX",
        "metric_type": "BM25",
        "inverted_index_algo": "DAAT_MAXSCORE",
        "bm25_k1": 1.2,
        "bm25_b": 0.75,
    },
}


class _SchemaMismatch(AppError):
    """The configured name points at a collection this adapter cannot serve."""

    def __init__(self, message: str = "Milvus schema does not match") -> None:
        super().__init__("service_unavailable", 503, message)


@dataclass(frozen=True, slots=True)
class IndexFingerprint:
    embedding: str
    tokenizer: str
    analyzer: str
    chunking: str
    schema: str

    def __post_init__(self) -> None:
        for name in ("embedding", "tokenizer", "analyzer", "chunking", "schema"):
            _bounded_string(getattr(self, name), name, _FINGERPRINT_BYTES)


@dataclass(frozen=True, slots=True)
class IndexRow:
    chunk_id: UUID
    repository_id: UUID
    text: str
    dense: list[float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunk_id", _uuid(self.chunk_id, "chunk_id"))
        object.__setattr__(
            self, "repository_id", _uuid(self.repository_id, "repository_id")
        )
        object.__setattr__(
            self, "text", _bounded_string(self.text, "text", MAX_TEXT_BYTES)
        )
        object.__setattr__(self, "dense", _dense_vector(self.dense))


@dataclass(frozen=True, slots=True)
class IndexFilter:
    repository_id: UUID
    index_version_id: UUID

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository_id", _uuid(self.repository_id, "repository_id")
        )
        object.__setattr__(
            self,
            "index_version_id",
            _uuid(self.index_version_id, "index_version_id"),
        )


@dataclass(frozen=True, slots=True)
class RankedHit:
    chunk_id: UUID
    index_version_id: UUID
    rank: int
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunk_id", _uuid(self.chunk_id, "chunk_id"))
        object.__setattr__(
            self,
            "index_version_id",
            _uuid(self.index_version_id, "index_version_id"),
        )
        if isinstance(self.rank, bool) or not isinstance(self.rank, int):
            raise TypeError("rank must be an integer")
        if self.rank < 1:
            raise ValueError("rank must be positive")
        score = _finite_float(self.score, "score")
        object.__setattr__(self, "score", score)


@dataclass(frozen=True, slots=True)
class RankedCandidates:
    bm25: tuple[RankedHit, ...]
    dense: tuple[RankedHit, ...]


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")
    return value


def _response_uuid(value: object, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise AppError(
                "service_unavailable", 503, f"Milvus returned invalid {name}"
            ) from error
    raise AppError("service_unavailable", 503, f"Milvus returned invalid {name}")


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise TypeError(f"{name} must be a number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _bounded_string(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be valid UTF-8") from error
    if len(encoded) > limit:
        raise ValueError(f"{name} is too long")
    return value


def _dense_vector(values: object) -> list[float]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("dense must be a sequence of numbers")
    if len(values) != DIMENSION:
        raise ValueError(f"dense must contain exactly {DIMENSION} values")
    vector: list[float] = []
    for value in values:
        if isinstance(value, (bool, str, bytes)):
            raise TypeError("dense values must be numbers")
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise TypeError("dense values must be numbers") from error
        if not math.isfinite(number):
            raise ValueError("dense values must be finite")
        vector.append(number)
    if not any(vector):
        raise ValueError("dense vector must be non-zero")
    return vector


def _decoded(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _strong_consistency(description: dict[str, Any]) -> bool:
    # Milvus 2.6 reports both the numeric level and its name; the name is
    # pinned because a missing name means this is not the collection shape the
    # adapter was verified against.
    return description.get("consistency_level_name") == "Strong"


def _number_matches(value: object, expected: float) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        return float(value) == expected  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return ()
    # PyMilvus returns protobuf repeated containers for function field names,
    # not built-in lists, so accept any non-mapping iterable.
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
        return ()
    return tuple(str(item) for item in value)


class MilvusIndex:
    """Synchronous Milvus adapter; callers own their asynchronous boundary."""

    def __init__(self, settings: Settings, *, client: object | None = None) -> None:
        self._settings = settings
        self._client = client
        self._ready = False
        self._fingerprint: IndexFingerprint | None = None

    @property
    def collection(self) -> str:
        return self._settings.milvus_collection

    @property
    def _connection(self) -> Any:
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except Exception as error:
                raise AppError(
                    "service_unavailable", 503, "Milvus client is unavailable"
                ) from error
            token = (
                self._settings.milvus_token.get_secret_value()
                if self._settings.milvus_token is not None
                else ""
            )
            try:
                self._client = MilvusClient(
                    uri=self._settings.milvus_uri,
                    token=token,
                    timeout=self._settings.milvus_timeout_seconds,
                )
            except Exception as error:
                raise AppError(
                    "service_unavailable", 503, "Milvus is unavailable"
                ) from error
        return self._client

    @property
    def _search_timeout(self) -> float:
        return min(float(self._settings.milvus_timeout_seconds), SEARCH_TIMEOUT_SECONDS)

    @property
    def _build_timeout(self) -> float:
        return float(self._settings.milvus_build_timeout_seconds)

    def _call(self, operation: Callable[[], Any], message: str) -> Any:
        try:
            return operation()
        except AppError:
            raise
        except Exception as error:
            raise AppError("service_unavailable", 503, message) from error

    def _require_ready(self) -> None:
        if not self._ready:
            raise AppError("service_unavailable", 503, "Milvus index is not ready")

    def ensure_schema(self, fingerprint: IndexFingerprint) -> None:
        """Create or validate the fixed schema and wait for it to be loaded."""
        if not isinstance(fingerprint, IndexFingerprint):
            raise TypeError("fingerprint must be an IndexFingerprint")
        if fingerprint.schema != SCHEMA_VERSION:
            raise AppError(
                "service_unavailable", 503, "Milvus schema version is not supported"
            )
        if fingerprint.analyzer != ANALYZER_VERSION:
            raise AppError(
                "service_unavailable", 503, "Milvus analyzer version is not supported"
            )
        if self._ready:
            return

        connection = self._connection
        timeout = self._build_timeout

        def prepare() -> None:
            if connection.has_collection(self.collection, timeout=timeout):
                self._validate_existing(connection, timeout)
            else:
                try:
                    self._create_collection(connection, timeout)
                except Exception:
                    # Another process may have won the creation race. Only
                    # accept that outcome after a fresh exact-schema check.
                    if not connection.has_collection(self.collection, timeout=timeout):
                        raise
                    self._validate_existing(connection, timeout)
            connection.load_collection(self.collection, timeout=timeout)
            indexes = set(connection.list_indexes(self.collection))
            if not {"dense", "sparse"} <= indexes:
                raise AppError(
                    "service_unavailable", 503, "Milvus indexes are incomplete"
                )

        self._call(prepare, "Milvus schema is unavailable")
        self._ready = True
        self._fingerprint = fingerprint

    def _create_collection(self, connection: Any, timeout: float) -> None:
        from pymilvus import DataType, Function, FunctionType

        schema = connection.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("repository_id", DataType.VARCHAR, max_length=36)
        schema.add_field("index_version_id", DataType.VARCHAR, max_length=36)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=36)
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=MAX_TEXT_BYTES,
            enable_analyzer=True,
            analyzer_params=dict(ANALYZER),
        )
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=DIMENSION)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="text_bm25",
                input_field_names=["text"],
                output_field_names=["sparse"],
                function_type=FunctionType.BM25,
            )
        )

        indexes = connection.prepare_index_params()
        indexes.add_index(
            "sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={
                "inverted_index_algo": "DAAT_MAXSCORE",
                "bm25_k1": 1.2,
                "bm25_b": 0.75,
            },
        )
        indexes.add_index(
            "dense",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        connection.create_collection(
            self.collection,
            schema=schema,
            index_params=indexes,
            consistency_level="Strong",
            timeout=timeout,
        )

    def _validate_existing(self, connection: Any, timeout: float) -> None:
        """Fail closed unless the existing collection is exactly ours.

        A collection with the configured name is not trusted by name alone:
        primary-key identity, schema options, consistency, vector fields and
        both index definitions must all match this adapter's contract.
        """
        from pymilvus import DataType, FunctionType

        description = self._call(
            lambda: connection.describe_collection(self.collection, timeout=timeout),
            "Milvus schema is unavailable",
        )
        if not isinstance(description, dict):
            raise _SchemaMismatch("Milvus schema is unavailable")
        if description.get("auto_id") is not False:
            raise _SchemaMismatch("Milvus auto_id does not match")
        if description.get("enable_dynamic_field") is not False:
            raise _SchemaMismatch("Milvus dynamic fields do not match")
        if not _strong_consistency(description):
            raise _SchemaMismatch("Milvus consistency does not match")

        fields = {
            field.get("name"): field
            for field in description.get("fields", [])
            if isinstance(field, dict)
        }
        expected = {
            "pk",
            "repository_id",
            "index_version_id",
            "chunk_id",
            "text",
            "dense",
            "sparse",
        }
        if set(fields) != expected:
            raise _SchemaMismatch("Milvus schema does not match")

        for name in ("repository_id", "index_version_id", "chunk_id"):
            self._require_type(fields[name], DataType.VARCHAR)
            self._require_max_length(fields[name], 36)
        self._require_type(fields["pk"], DataType.VARCHAR)
        self._require_max_length(fields["pk"], 64)
        if fields["pk"].get("is_primary") is not True:
            raise _SchemaMismatch("Milvus primary key does not match")

        text = fields["text"]
        self._require_type(text, DataType.VARCHAR)
        self._require_max_length(text, MAX_TEXT_BYTES)
        params = text.get("params") or {}
        if not _truthy(params.get("enable_analyzer")):
            raise _SchemaMismatch("Milvus analyzer is not enabled")
        if _decoded(params.get("analyzer_params")) != ANALYZER:
            raise _SchemaMismatch("Milvus analyzer does not match")

        dense = fields["dense"]
        self._require_type(dense, DataType.FLOAT_VECTOR)
        dense_params = dense.get("params") or {}
        try:
            dimension = int(dense_params.get("dim", -1))
        except (TypeError, ValueError) as error:
            raise _SchemaMismatch("Milvus schema does not match") from error
        if dimension != DIMENSION:
            raise _SchemaMismatch("Milvus schema does not match")

        self._require_type(fields["sparse"], DataType.SPARSE_FLOAT_VECTOR)
        functions = description.get("functions") or []
        if not any(
            isinstance(function, dict)
            and int(function.get("type", -1)) == int(FunctionType.BM25)
            and _names(function.get("input_field_names")) == ("text",)
            and _names(function.get("output_field_names")) == ("sparse",)
            for function in functions
        ):
            raise _SchemaMismatch("Milvus BM25 function does not match")

        listed = self._call(
            lambda: connection.list_indexes(self.collection),
            "Milvus indexes are unavailable",
        )
        if (
            isinstance(listed, (str, bytes))
            or not isinstance(listed, Iterable)
            or set(listed) != set(INDEX_CONTRACT)
        ):
            raise _SchemaMismatch("Milvus indexes do not match")
        for name, spec in INDEX_CONTRACT.items():
            detail = self._call(
                lambda name=name: connection.describe_index(
                    self.collection, name, timeout=timeout
                ),
                "Milvus index metadata is unavailable",
            )
            if not isinstance(detail, dict):
                raise _SchemaMismatch("Milvus index metadata does not match")
            if detail.get("field_name") not in (None, name):
                raise _SchemaMismatch("Milvus index field does not match")
            if detail.get("index_name") not in (None, name):
                raise _SchemaMismatch("Milvus index name does not match")
            for key, expected_value in spec.items():
                value = detail.get(key)
                if isinstance(expected_value, str):
                    if value != expected_value:
                        raise _SchemaMismatch("Milvus index parameters do not match")
                elif not _number_matches(value, expected_value):
                    raise _SchemaMismatch("Milvus index parameters do not match")

    @staticmethod
    def _require_type(field: dict[str, object], expected: object) -> None:
        try:
            actual = int(field.get("type", -1))  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise _SchemaMismatch() from error
        if actual != int(expected):  # type: ignore[call-overload]
            raise _SchemaMismatch()

    @staticmethod
    def _require_max_length(field: dict[str, object], expected: int) -> None:
        params = field.get("params") or {}
        try:
            actual = int(params.get("max_length", -1))  # type: ignore[union-attr]
        except (TypeError, ValueError) as error:
            raise _SchemaMismatch() from error
        if actual != expected:
            raise _SchemaMismatch()

    @staticmethod
    def _primary_key(index_id: UUID, chunk_id: UUID) -> str:
        return hashlib.sha256(f"{index_id}:{chunk_id}".encode()).hexdigest()

    def upsert(self, index_id: UUID, rows: Sequence[IndexRow]) -> None:
        index_id = _uuid(index_id, "index_id")
        self._require_ready()
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise TypeError("rows must be a sequence of IndexRow")
        if len(rows) > MAX_UPSERT_ROWS:
            raise ValueError(f"rows must contain at most {MAX_UPSERT_ROWS} items")

        unique: dict[UUID, dict[str, object]] = {}
        for row in rows:
            if not isinstance(row, IndexRow):
                raise TypeError("rows must contain IndexRow values")
            record = {
                "pk": self._primary_key(index_id, row.chunk_id),
                "repository_id": str(row.repository_id),
                "index_version_id": str(index_id),
                "chunk_id": str(row.chunk_id),
                "text": row.text,
                "dense": row.dense,
            }
            previous = unique.get(row.chunk_id)
            if previous is not None:
                if previous != record:
                    raise ValueError("duplicate chunk rows conflict")
                continue
            unique[row.chunk_id] = record
        if not unique:
            return

        data = list(unique.values())
        self._call(
            lambda: self._connection.upsert(
                self.collection, data, timeout=self._build_timeout
            ),
            "Milvus upsert failed",
        )

    def probe(
        self,
        fingerprint: IndexFingerprint,
        index_id: UUID,
        chunk_ids: Sequence[UUID],
        *,
        expected_count: int | None = None,
    ) -> str | None:
        """Bounded read-only readiness check with an explicit failure reason.

        Unlike ``ensure_schema`` this never creates or loads a collection, so it
        is safe on a read path. A missing or incompatible collection is an index
        state, not an infrastructure outage; transport errors still raise.
        An exact ``expected_count`` makes vector loss outside the bounded ID
        sample observable rather than a source of false readiness.
        """
        if not isinstance(fingerprint, IndexFingerprint):
            raise TypeError("fingerprint must be an IndexFingerprint")
        index_id = _uuid(index_id, "index_id")
        if expected_count is not None and (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 0
        ):
            raise TypeError("expected_count must be a non-negative integer")
        if fingerprint.schema != SCHEMA_VERSION or (
            fingerprint.analyzer != ANALYZER_VERSION
        ):
            return "schema_unsupported"
        timeout = self._build_timeout
        collection = self.collection
        present = self._call(
            lambda: self._connection.has_collection(collection, timeout=timeout),
            "Milvus readiness probe failed",
        )
        if not present:
            return "collection_missing"
        try:
            self._validate_existing(self._connection, timeout)
        except _SchemaMismatch:
            return "schema_mismatch"
        if expected_count is not None:
            observed = self._count(self._connection, index_id)
            if observed != expected_count:
                return "vectors_missing"
        visible = self._visible(self._connection, index_id, chunk_ids)
        return None if visible else "vectors_missing"

    def _count(self, connection: Any, index_id: UUID) -> int:
        """Exact version-scoped row count under Strong consistency."""
        expression = f"index_version_id == {json.dumps(str(index_id))}"
        rows = self._call(
            lambda: connection.query(
                self.collection,
                filter=expression,
                output_fields=["count(*)"],
                consistency_level="Strong",
                timeout=self._build_timeout,
            ),
            "Milvus count check failed",
        )
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            raise AppError("service_unavailable", 503, "Milvus returned invalid count")
        value = rows[0].get("count(*)")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AppError("service_unavailable", 503, "Milvus returned invalid count")
        return value

    def _visible(
        self, connection: Any, index_id: UUID, chunk_ids: Sequence[UUID]
    ) -> bool:
        unique: list[UUID] = []
        seen: set[UUID] = set()
        for chunk_id in chunk_ids:
            value = _uuid(chunk_id, "chunk_id")
            if value not in seen:
                seen.add(value)
                unique.append(value)
        if not unique:
            return True

        for start in range(0, len(unique), VERIFY_BATCH_SIZE):
            batch = unique[start : start + VERIFY_BATCH_SIZE]
            expression = self._visibility_expression(index_id, batch)
            rows = self._call(
                lambda expression=expression, batch=batch: connection.query(
                    self.collection,
                    filter=expression,
                    output_fields=["chunk_id", "index_version_id"],
                    consistency_level="Strong",
                    limit=len(batch),
                    timeout=self._build_timeout,
                ),
                "Milvus visibility check failed",
            )
            if not isinstance(rows, list):
                raise AppError(
                    "service_unavailable", 503, "Milvus returned invalid rows"
                )
            observed: set[UUID] = set()
            expected = set(batch)
            for row in rows:
                if not isinstance(row, dict):
                    raise AppError(
                        "service_unavailable", 503, "Milvus returned invalid rows"
                    )
                chunk_id = _response_uuid(row.get("chunk_id"), "chunk_id")
                version = _response_uuid(
                    row.get("index_version_id"), "index_version_id"
                )
                if version != index_id or chunk_id not in expected:
                    raise AppError(
                        "service_unavailable", 503, "Milvus returned unexpected rows"
                    )
                observed.add(chunk_id)
            if observed != expected:
                return False
        return True

    def verify_visible(self, index_id: UUID, chunk_ids: Sequence[UUID]) -> bool:
        """Check every exact chunk ID with Strong consistency in small batches."""
        index_id = _uuid(index_id, "index_id")
        self._require_ready()
        if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, Sequence):
            raise TypeError("chunk_ids must be a sequence of UUID")
        return self._visible(self._connection, index_id, chunk_ids)

    @staticmethod
    def _visibility_expression(index_id: UUID, chunk_ids: Sequence[UUID]) -> str:
        values = ", ".join(json.dumps(str(chunk_id)) for chunk_id in chunk_ids)
        return (
            f"index_version_id == {json.dumps(str(index_id))} "
            f"and chunk_id in [{values}]"
        )

    def search(
        self,
        query_text: str,
        query_vector: list[float],
        filters: tuple[IndexFilter, ...],
        *,
        limit: int = 30,
    ) -> RankedCandidates:
        """Run BM25 and dense searches under the exact same server-side scope."""
        self._require_ready()
        query_text = _bounded_string(query_text, "query_text", MAX_TEXT_BYTES)
        vector = _dense_vector(query_vector)
        if not isinstance(filters, tuple) or not filters:
            raise ValueError("filters must be a non-empty tuple")
        if len(filters) > MAX_FILTERS:
            raise ValueError(f"filters must contain at most {MAX_FILTERS} items")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")

        unique: list[IndexFilter] = []
        for item in filters:
            if not isinstance(item, IndexFilter):
                raise TypeError("filters must contain IndexFilter values")
            if item not in unique:
                unique.append(item)
        expression = self._filter_expression(unique)
        allowed = {(item.repository_id, item.index_version_id) for item in unique}

        bm25 = self._search_route(
            data=[query_text],
            field="sparse",
            params={"metric_type": "BM25"},
            expression=expression,
            allowed=allowed,
            limit=limit,
        )
        dense = self._search_route(
            data=[vector],
            field="dense",
            params={
                "metric_type": "COSINE",
                "params": {"ef": min(256, max(64, limit * 2))},
            },
            expression=expression,
            allowed=allowed,
            limit=limit,
        )
        return RankedCandidates(bm25=bm25, dense=dense)

    @staticmethod
    def _filter_expression(filters: Sequence[IndexFilter]) -> str:
        return " or ".join(
            f"(repository_id == {json.dumps(str(item.repository_id))} "
            f"and index_version_id == {json.dumps(str(item.index_version_id))})"
            for item in filters
        )

    def _search_route(
        self,
        *,
        data: list[object],
        field: str,
        params: dict[str, object],
        expression: str,
        allowed: set[tuple[UUID, UUID]],
        limit: int,
    ) -> tuple[RankedHit, ...]:
        raw = self._call(
            lambda: self._connection.search(
                self.collection,
                data=data,
                anns_field=field,
                search_params=params,
                limit=limit,
                filter=expression,
                output_fields=[
                    "repository_id",
                    "chunk_id",
                    "index_version_id",
                ],
                consistency_level="Strong",
                timeout=self._search_timeout,
            ),
            "Milvus search failed",
        )
        if not isinstance(raw, list) or len(raw) != 1:
            raise AppError(
                "service_unavailable", 503, "Milvus returned invalid search results"
            )
        hits = raw[0]
        if not isinstance(hits, list):
            raise AppError(
                "service_unavailable", 503, "Milvus returned invalid search results"
            )

        ranked: list[RankedHit] = []
        seen: set[UUID] = set()
        try:
            for hit in hits:
                fields = self._hit_fields(hit)
                chunk_id = _response_uuid(fields.get("chunk_id"), "chunk_id")
                version = _response_uuid(
                    fields.get("index_version_id"), "index_version_id"
                )
                repository = _response_uuid(
                    fields.get("repository_id"), "repository_id"
                )
                if (repository, version) not in allowed:
                    raise AppError(
                        "service_unavailable",
                        503,
                        "Milvus returned out-of-scope rows",
                    )
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                ranked.append(
                    RankedHit(
                        chunk_id=chunk_id,
                        index_version_id=version,
                        rank=len(ranked) + 1,
                        score=self._hit_score(hit),
                    )
                )
        except AppError:
            raise
        except (TypeError, ValueError) as error:
            raise AppError(
                "service_unavailable", 503, "Milvus returned invalid search results"
            ) from error
        return tuple(ranked)

    @staticmethod
    def _hit_fields(hit: object) -> dict[str, object]:
        entity = (
            hit.get("entity") if isinstance(hit, dict) else getattr(hit, "entity", None)
        )
        if isinstance(entity, dict):
            return entity
        if entity is not None:
            return {
                name: getattr(entity, name, None)
                for name in ("repository_id", "chunk_id", "index_version_id")
            }
        if isinstance(hit, dict):
            return hit
        raise AppError(
            "service_unavailable", 503, "Milvus returned invalid search results"
        )

    @staticmethod
    def _hit_score(hit: object) -> float:
        value = (
            hit.get("distance")
            if isinstance(hit, dict)
            else getattr(hit, "distance", None)
        )
        if value is None:
            raise AppError("service_unavailable", 503, "Milvus returned invalid scores")
        return _finite_float(value, "score")

    def delete_version(self, index_id: UUID) -> None:
        index_id = _uuid(index_id, "index_id")
        self._require_ready()
        expression = f"index_version_id == {json.dumps(str(index_id))}"
        self._call(
            lambda: self._connection.delete(
                self.collection, filter=expression, timeout=self._build_timeout
            ),
            "Milvus delete failed",
        )

    def purge_version(self, index_id: UUID) -> bool:
        """Delete one version's rows without creating or loading a collection.

        Retention must never recreate a collection that an operator dropped:
        an absent collection already means the vectors are unreachable. Returns
        False in that case so the caller can record the purge. Transport and
        delete failures raise, leaving the version retryable.
        """
        index_id = _uuid(index_id, "index_id")
        timeout = self._build_timeout
        present = self._call(
            lambda: self._connection.has_collection(self.collection, timeout=timeout),
            "Milvus purge probe failed",
        )
        if not present:
            return False
        expression = f"index_version_id == {json.dumps(str(index_id))}"
        self._call(
            lambda: self._connection.delete(
                self.collection, filter=expression, timeout=timeout
            ),
            "Milvus delete failed",
        )
        return True
