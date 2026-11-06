"""Server-owned citation identities, exact URLs and bounded source previews.

A citation ID is a deterministic prefix of the SHA-256 digest over the immutable
evidence identity and source interval. Rank, result order and model output are
never inputs, so reusing the same evidence always yields the same ID. The
server, not a model, owns the URL and the preview authorization boundary.
"""

import hashlib
import json
import re
from collections import Counter
from urllib.parse import quote

from sqlalchemy import select

from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.knowledge.schemas import (
    CitationEntry,
    EvidenceManifest,
    EvidenceSet,
    SourceIdentity,
)
from devflow.knowledge.sources import SourceReader
from devflow.repositories.models import Repository

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PREVIEW_LINES = 500


def assign_ids(digests):
    """Assign stable short IDs, lengthening on collision, never merging."""
    if not all(
        isinstance(value, str) and _DIGEST.fullmatch(value) for value in digests
    ):
        raise AppError("internal_error", 500, "Invalid citation identity")
    length = 16
    while True:
        candidates = tuple(digest[:length] for digest in digests)
        if all(count == 1 for count in Counter(candidates).values()):
            return candidates
        if length == 64:
            raise AppError("internal_error", 500, "Citation identity collision")
        length = min(64, length + 8)


def identity_digest(identity: SourceIdentity, location) -> str:
    return hashlib.sha256(
        json.dumps(
            [identity.model_dump(mode="json"), location.model_dump(mode="json")],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _fragment(location) -> str:
    if location.end_line == location.start_line:
        return f"#L{location.start_line}"
    return f"#L{location.start_line}-L{location.end_line}"


class CitationService:
    def __init__(self, factory, policy, *, public_base_url=None, reader=None):
        self.factory, self.policy = factory, policy
        self.public_base_url = (
            policy.settings.public_base_url
            if public_base_url is None
            else public_base_url
        ).rstrip("/")
        self.reader = reader

    async def _names(self, repository_ids):
        if not repository_ids:
            return {}
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(Repository.id, Repository.full_name).where(
                        Repository.id.in_(repository_ids)
                    )
                )
            ).all()
        names = dict(rows)
        if set(names) != set(repository_ids):
            raise AppError("internal_error", 500, "Citation source unavailable")
        return names

    def _url(self, identity: SourceIdentity, location, names) -> str:
        if identity.workspace_id is None:
            base = (
                f"{self.public_base_url}/api/v1/repositories/"
                f"{identity.repository_id}/knowledge/documents/"
                f"{identity.document_id}"
            )
            return base + _fragment(location)
        name = names[identity.source_repository_id]
        encoded = "/".join(quote(part, safe="") for part in identity.path.split("/"))
        return (
            "https://github.com/"
            + "/".join(quote(part, safe="") for part in name.split("/"))
            + "/blob/"
            + identity.sha
            + "/"
            + encoded
            + _fragment(location)
        )

    async def manifest(self, evidence: EvidenceSet) -> EvidenceManifest:
        items = evidence.evidence
        ids = assign_ids(
            [identity_digest(item.identity, item.location) for item in items]
        )
        names = await self._names(
            {item.identity.source_repository_id for item in items}
        )
        return EvidenceManifest(
            entries=tuple(
                CitationEntry(
                    citation_id=citation_id,
                    identity=item.identity,
                    location=item.location,
                    url=self._url(item.identity, item.location, names),
                    content_hash=item.content_hash,
                    rank=item.rank,
                )
                for item, citation_id in zip(items, ids, strict=True)
            )
        )

    def validate_ids(self, manifest: EvidenceManifest, ids) -> None:
        known = {entry.citation_id for entry in manifest.entries}
        for value in ids:
            if not isinstance(value, str) or value not in known:
                raise AppError(
                    "answer_validation_failed", 422, "Unknown citation reference"
                )

    async def preview(
        self, actor, manifest: EvidenceManifest, citation_id, *, cursor=None
    ):
        entry = next(
            (item for item in manifest.entries if item.citation_id == citation_id),
            None,
        )
        if entry is None:
            raise not_found()
        reader = self.reader or SourceReader(self.factory, self.policy)
        return await reader.preview(
            actor,
            entry.identity,
            start_line=entry.location.start_line,
            line_count=min(
                _PREVIEW_LINES,
                entry.location.end_line - entry.location.start_line + 1,
            ),
            cursor=cursor,
        )
