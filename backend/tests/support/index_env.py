"""Deterministic fixtures for the versioned knowledge-index tasks.

The fakes never claim model or Milvus acceptance. They make publication and
retention rules observable; real inference and real Milvus stay NOT EXECUTED.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select

from devflow.auth.models import User
from devflow.github.models import GitHubInstallation
from devflow.jobs.models import Job
from devflow.knowledge.inference import ModelFingerprint
from devflow.knowledge.milvus import (
    ANALYZER_VERSION,
    DIMENSION,
    SCHEMA_VERSION,
    IndexFingerprint,
    IndexRow,
)
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    IndexMember,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
    RevisionDependency,
)
from devflow.knowledge.sources import workspace_source_version
from devflow.repositories.models import Repository

TOKENIZER = "a" * 64
CHUNKING = "chunking-v1"
SHA = "b" * 40
NEXT_SHA = "c" * 40
EMBEDDING = "fake-embedding-v1"
MODEL_TOKENIZER = "fake-tokenizer-v1"


def fingerprint() -> IndexFingerprint:
    return IndexFingerprint(
        embedding=EMBEDDING,
        tokenizer=MODEL_TOKENIZER,
        analyzer=ANALYZER_VERSION,
        chunking=CHUNKING,
        schema=SCHEMA_VERSION,
    )


def digest(rows) -> str:
    return sha256(
        "".join(f"{chunk}:{ordinal}\n" for chunk, ordinal in rows).encode()
    ).hexdigest()


class FakeInference:
    """A deterministic embedder; it proves plumbing, never model quality."""

    def __init__(self):
        self.started = 0
        self.embedded = 0

    async def start(self) -> ModelFingerprint:
        self.started += 1
        return ModelFingerprint(
            embedding=EMBEDDING,
            reranker="fake-reranker-v1",
            tokenizer=MODEL_TOKENIZER,
            dimension=DIMENSION,
            device="cpu",
            dtype="float32",
        )

    async def embed(self, texts):
        self.embedded += len(texts)
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [0.0] * DIMENSION
        seed = int.from_bytes(sha256(text.encode()).digest()[:8], "big")
        values[seed % DIMENSION] = 1.0
        return values


class FakeMilvus:
    """Version-scoped rows with deterministic visibility and delete failure."""

    def __init__(self):
        self.rows: dict[UUID, dict[UUID, IndexRow]] = {}
        self.upserts: list[tuple[UUID, list[UUID]]] = []
        self.deleted: list[UUID] = []
        self.visible = True
        self.fail_deletes = 0
        self.collection_present = True
        self.schema_calls = 0

    def ensure_schema(self, value: IndexFingerprint) -> None:
        self.schema_calls += 1
        self.fingerprint = value

    def upsert(self, index_id: UUID, rows) -> None:
        bucket = self.rows.setdefault(index_id, {})
        for row in rows:
            bucket[row.chunk_id] = row
        self.upserts.append((index_id, [row.chunk_id for row in rows]))

    def verify_visible(self, index_id: UUID, chunk_ids) -> bool:
        if not self.visible:
            return False
        bucket = self.rows.get(index_id, {})
        return all(chunk in bucket for chunk in chunk_ids)

    def probe(self, fingerprint, index_id, chunk_ids, *, expected_count=None):
        if not self.collection_present:
            return "collection_missing"
        bucket = self.rows.get(index_id, {})
        if expected_count is not None and len(bucket) != expected_count:
            return "vectors_missing"
        if not self.visible or any(chunk not in bucket for chunk in chunk_ids):
            return "vectors_missing"
        return None

    def delete_version(self, index_id: UUID) -> None:
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise RuntimeError("milvus delete unavailable")
        self.deleted.append(index_id)
        self.rows.pop(index_id, None)

    def purge_version(self, index_id: UUID) -> bool:
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise RuntimeError("milvus delete unavailable")
        if not self.collection_present:
            return False
        self.deleted.append(index_id)
        self.rows.pop(index_id, None)
        return True


@dataclass(frozen=True)
class ReadyImport:
    import_id: UUID
    job_id: UUID
    target_generation: int
    target_sha: str
    revision_ids: tuple[UUID, ...]
    chunk_ids: tuple[UUID, ...]
    document_paths: tuple[str, ...]
    tokenizer_fingerprint: str
    chunking_version: str
    deadline: datetime


async def ready_import(
    env,
    *,
    generation: int | None = None,
    sha: str = SHA,
    chunk_count: int = 2,
    path_prefix: str = "docs",
) -> ReadyImport:
    """Create a completed composed import and move the corpus target to it.

    A later call with a higher generation models a workspace re-import that
    supersedes the older snapshot while retaining its provenance rows.
    """
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=24)
    import_id = uuid4()
    with_body = chunk_count > 0
    # A searchable revision must carry a coherent Git identity; the workspace
    # version is created first so the revision can reference it.
    version = await env.workspace(sha=sha) if with_body else None
    async with env.factory.begin() as db:
        if generation is None:
            previous = await db.scalar(
                select(func.max(KnowledgeImport.target_generation)).where(
                    KnowledgeImport.repository_id == env.repository.id
                )
            )
            generation = (previous or 0) + 1
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        if corpus is None:
            corpus = KnowledgeCorpus(repository_id=env.repository.id)
            db.add(corpus)
        corpus.target_generation = generation
        corpus.target_workspace_id = None
        corpus.target_sha = sha
        corpus.path_rule_version = "knowledge-paths-v1"
        corpus.path_scopes = {}
        user = await db.get(User, env.actor.id)
        repository = await db.get(Repository, env.repository.id)
        installation = await db.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == repository.installation_id
            )
        )
        job = Job(
            id=uuid4(),
            kind="knowledge.import",
            repository_id=repository.id,
            requested_by=user.id,
            input={"import_id": str(import_id), "target_generation": generation},
            input_hash=sha256(f"{import_id}:{generation}".encode()).hexdigest(),
            idempotency_key=f"seed-{uuid4().hex}",
            status="succeeded",
        )
        db.add(job)
        await db.flush()
        imported = KnowledgeImport(
            id=import_id,
            repository_id=repository.id,
            source_repository_id=repository.id,
            actor_id=user.id,
            actor_github_id=user.github_id,
            actor_auth_version=user.auth_version,
            kind="workspace",
            idempotency_key=f"import-{uuid4().hex}",
            request_hash=sha256(f"request-{import_id}".encode()).hexdigest(),
            request={},
            target_generation=generation,
            workspace_id=None,
            sha=sha,
            path_rule_version="knowledge-paths-v1",
            job_id=job.id,
            status="ready",
            created_at=now,
            deadline=deadline,
            cursor=1 if with_body else 0,
            included=1 if with_body else 0,
            skipped=0,
            unsupported=0,
            over_limit=0,
            scanned_bytes=1024 if with_body else 0,
            failures=0,
            unscanned=False,
            complete=True,
            composition_version=1,
            target_workspace_id=None,
            target_sha=sha,
            target_path_scopes={},
            uploads_after=None,
            uploads_done=True,
            workspace_cursor=1 if with_body else 0,
            workspace_done=True,
            eligible_files=1 if with_body else 0,
            processed_chunks=chunk_count,
            tokenizer_fingerprint=TOKENIZER,
            chunking_version=CHUNKING,
        )
        db.add(imported)
        await db.flush()
        revision_ids: list[UUID] = []
        chunk_ids: list[UUID] = []
        document_paths: list[str] = []
        member = None
        if with_body:
            path = f"{path_prefix}/{import_id.hex}.md"
            body = f"# indexed document\n\n{sha}\n".encode()
            revision = DocumentRevision(
                id=uuid4(),
                revision_hash=sha256(body).hexdigest(),
                source_version=workspace_source_version(
                    repository_id=repository.id,
                    source_repository_id=version.source_repository_id,
                    dependency_repository_ids=(repository.id,),
                    workspace_id=version.id,
                    sha=version.commit_sha,
                    path=path,
                ),
                workspace_id=version.id,
                sha=version.commit_sha,
                body=body,
                body_bytes=len(body),
                searchable=True,
                availability="available",
                published_at=now,
            )
            document = Document(
                id=uuid4(),
                repository_id=repository.id,
                source_repository_id=version.source_repository_id,
                kind="workspace",
                path=path,
                attributes={},
            )
            document.id = document.id or uuid4()
            revision.document_id = document.id
            db.add_all([document, revision])
            await db.flush()
            db.add(
                RevisionDependency(revision_id=revision.id, repository_id=repository.id)
            )
            member = ImportMember(
                id=uuid4(),
                import_id=import_id,
                source_key=path,
                ordinal=0,
                revision_id=revision.id,
                status="included",
                member_kind="workspace",
                next_chunk=chunk_count,
                total_chunks=chunk_count,
                declared_bytes=len(body),
            )
            db.add(member)
            for ordinal in range(chunk_count):
                text = f"indexed chunk {ordinal} for {path}"
                chunk = Chunk(
                    id=uuid4(),
                    revision_id=revision.id,
                    ordinal=ordinal,
                    text=text,
                    embedding_text=f"{path}\n{text}",
                    content_hash=sha256(text.encode()).hexdigest(),
                    token_count=3,
                    tokenizer_fingerprint=TOKENIZER,
                    chunking_version=CHUNKING,
                    start_line=ordinal + 1,
                    end_line=ordinal + 1,
                    start_byte=ordinal,
                    end_byte=ordinal + len(text),
                    attributes={},
                )
                db.add(chunk)
                chunk_ids.append(chunk.id)
            revision_ids.append(revision.id)
            document_paths.append(path)
        db.add(
            ImportRepositoryEpoch(
                import_id=import_id,
                repository_id=repository.id,
                installation_id=repository.installation_id,
                user_version=user.auth_version,
                installation_version=installation.auth_version,
                repository_version=repository.auth_version,
            )
        )
        await db.flush()
        assert member is None or member.total_chunks == chunk_count
    return ReadyImport(
        import_id=import_id,
        job_id=job.id,
        target_generation=generation,
        target_sha=sha,
        revision_ids=tuple(revision_ids),
        chunk_ids=tuple(chunk_ids),
        document_paths=tuple(document_paths),
        tokenizer_fingerprint=TOKENIZER,
        chunking_version=CHUNKING,
        deadline=deadline,
    )


async def authorization_for(env) -> dict:
    now = datetime.now(UTC)
    async with env.factory() as db:
        user = await db.get(User, env.actor.id)
        repository = await db.get(Repository, env.repository.id)
        installation = await db.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == repository.installation_id
            )
        )
    return {
        "actor": {
            "id": str(user.id),
            "github_id": user.github_id,
            "auth_version": user.auth_version,
            "role": user.role,
        },
        "repositories": [
            {
                "repository_id": str(repository.id),
                "installation_id": repository.installation_id,
                "user_version": user.auth_version,
                "installation_version": installation.auth_version,
                "repository_version": repository.auth_version,
            }
        ],
        "recorded_at": now.isoformat(),
    }


async def make_index(
    env,
    fixture: ReadyImport,
    *,
    status: str = "retired",
    chunk_ids=None,
    created_days_ago: float = 0.0,
    target_generation: int | None = None,
    target_sha: str | None = None,
    queryable: bool | None = None,
) -> IndexVersion:
    """Insert one index version without exercising publication."""
    if chunk_ids is None:
        chunk_ids = [uuid4() for _ in range(2)]
    chunk_ids = list(chunk_ids)
    now = datetime.now(UTC)
    created_at = now - timedelta(days=created_days_ago)
    index_id = uuid4()
    if queryable is None:
        queryable = status == "ready"
    authorization = await authorization_for(env)
    async with env.factory.begin() as db:
        job = Job(
            id=uuid4(),
            kind="knowledge.index_batch",
            repository_id=env.repository.id,
            requested_by=env.actor.id,
            input={"version": 1, "index_id": str(index_id)},
            input_hash=sha256(f"index-{index_id}".encode()).hexdigest(),
            idempotency_key=uuid4().hex,
            status="succeeded" if status in {"ready", "retired"} else "running",
        )
        db.add(job)
        row = IndexVersion(
            id=index_id,
            repository_id=env.repository.id,
            import_id=fixture.import_id,
            job_id=job.id,
            requested_by=env.actor.id,
            target_generation=target_generation or fixture.target_generation,
            target_sha=target_sha or fixture.target_sha,
            path_rule_version="knowledge-paths-v1",
            path_scopes={},
            idempotency_key=uuid4().hex,
            request_hash=sha256(f"request-{index_id}".encode()).hexdigest(),
            authorization=authorization,
            status=status,
            fingerprint={
                "embedding": EMBEDDING,
                "tokenizer": MODEL_TOKENIZER,
                "analyzer": ANALYZER_VERSION,
                "chunking": CHUNKING,
                "schema": SCHEMA_VERSION,
            },
            expected_member_count=len(chunk_ids),
            written_count=len(chunk_ids),
            member_digest=digest(
                (chunk, ordinal) for ordinal, chunk in enumerate(chunk_ids)
            ),
            queryable=queryable,
            created_at=created_at,
            deadline=created_at + timedelta(hours=24),
            failures=0,
            updated_at=created_at,
            published_at=created_at if status in {"ready", "retired"} else None,
            retired_at=created_at if status == "retired" else None,
        )
        db.add(row)
        await db.flush()
        for ordinal, chunk_id in enumerate(chunk_ids):
            db.add(
                IndexMember(
                    id=uuid4(),
                    index_version_id=index_id,
                    chunk_id=chunk_id,
                    ordinal=ordinal,
                    status="written",
                    written_at=created_at,
                )
            )
        await db.flush()
    return row


async def set_active(env, index_id: UUID) -> None:
    async with env.factory.begin() as db:
        corpus = await db.get(KnowledgeCorpus, env.repository.id, with_for_update=True)
        corpus.active_index_version_id = index_id
