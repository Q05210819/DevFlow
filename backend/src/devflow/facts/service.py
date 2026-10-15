"""Bounded, purpose-bound facts with a post-materialization authorization fence."""

import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from devflow.errors import AppError
from devflow.facts.models import Fact, FactCollection
from devflow.facts.schemas import (
    CollectionView,
    FactCoverage,
    FactDetail,
    FactPage,
    FactView,
)
from devflow.github.client import not_found
from devflow.jobs.service import JobService
from devflow.sync.models import SyncCursor
from devflow.sync.scope import transaction

COLLECTIONS = {
    "issue": {"comments": "comment"},
    "pull": {
        "comments": "comment",
        "reviews": "review",
        "review_comments": "review_comment",
        "files": "pr_file",
    },
    "ci_run": {"attempts": "ci_run", "jobs": "ci_job"},
}


def source_url(row, full_name, parent_number=None):
    """Only canonical correspondence can turn persisted provider URLs into links."""
    try:
        url = urlsplit(row.source_url)
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.query
        ):
            return None
        number = row.number or parent_number
        browser, api = [], []
        if row.kind == "issue":
            browser, api = [f"issues/{number}"], [f"issues/{number}"]
        elif row.kind == "pull":
            browser, api = [f"pull/{number}"], [f"pulls/{number}"]
        elif row.kind == "ci_run":
            browser = [
                f"actions/runs/{row.source_id}",
                f"actions/runs/{row.source_id}/attempts/{row.payload['attempt']}",
            ]
            api = [
                f"actions/runs/{row.source_id}",
                f"actions/runs/{row.source_id}/attempts/{row.payload['attempt']}",
            ]
        elif row.kind == "ci_job":
            browser = [
                f"actions/runs/{row.parent_source_id}/job/{row.source_id}",
            ]
            api = [f"actions/jobs/{row.source_id}"]
        elif number:
            parent = "issues" if row.parent_kind == "issue" else "pull"
            fragments = {
                "comment": f"issuecomment-{row.source_id}",
                "review": f"pullrequestreview-{row.source_id}",
                "review_comment": f"discussion_r{row.source_id}",
            }
            browser = [f"{parent}/{number}"]
            api = {
                "comment": [f"issues/comments/{row.source_id}"],
                "review": [f"pulls/{number}/reviews/{row.source_id}"],
                "review_comment": [f"pulls/comments/{row.source_id}"],
                "pr_file": [f"pulls/{number}/files"],
            }[row.kind]
            if row.kind == "pr_file":
                browser = [f"pull/{number}/files"]
            elif url.fragment and url.fragment != fragments[row.kind]:
                return None
        if url.hostname == "github.com" and url.path in [
            f"/{full_name}/{p}" for p in browser
        ]:
            if url.fragment and not re.fullmatch(
                r"(?:issuecomment-|pullrequestreview-|discussion_r|diff-)[a-zA-Z0-9]+",
                url.fragment,
            ):
                return None
            return row.source_url
        if (
            url.hostname == "api.github.com"
            and not url.fragment
            and url.path in [f"/repos/{full_name}/{p}" for p in api]
        ):
            fragment = {
                "comment": f"#issuecomment-{row.source_id}",
                "review": f"#pullrequestreview-{row.source_id}",
                "review_comment": f"#discussion_r{row.source_id}",
            }.get(row.kind, "")
            return f"https://github.com/{full_name}/{browser[-1]}{fragment}"
    except (ValueError, KeyError):
        pass
    return None


def fact_view(row, full_name, parent_number=None):
    return FactView(
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "kind",
                "source_id",
                "number",
                "version_key",
                "source_updated_at",
                "observed_at",
                "unavailable",
                "payload",
            )
        },
        source_url=source_url(row, full_name, parent_number),
    )


async def coverage(db, repository_id, kind):
    stream = {"issue": "issues", "pull": "pulls", "ci_run": "actions"}[kind]
    row = await db.get(SyncCursor, (repository_id, stream))
    return FactCoverage(
        stream=stream,
        status=row.status if row else "not_synced",
        **(
            {
                "start": row.covered_from,
                "end": row.covered_until,
                "completed_watermark": row.completed_watermark,
                "last_success_at": row.last_success_at,
                "error_code": row.error_code,
            }
            if row
            else {}
        ),
    )


async def recheck(policy, actor, repository_id, key):
    try:
        _, current = await policy.local(actor, repository_id)
        if current != key:
            raise not_found()
    except AppError as error:
        await policy.audit_denial(actor, error, repository_id)
        raise


class FactService:
    def __init__(self, factory, policy, codec):
        self.factory, self.policy, self.codec = factory, policy, codec

    async def _authorize(self, actor, repo):
        key = await JobService(self.factory, self.policy)._authorize(
            actor, repo, fresh=False
        )
        # Require already verified this binding; local is not a substitute for it.
        async with self.factory() as db:
            from devflow.repositories.models import Repository

            name = await db.scalar(
                select(Repository.full_name).where(Repository.id == repo)
            )
        return name, key

    async def list(self, actor, repo, kind, *, cursor=None, limit=30, filters=None):
        return await self._read(
            actor, repo, kind, cursor=cursor, limit=limit, filters=filters
        )

    async def detail(
        self,
        actor,
        repo,
        kind,
        number_or_id,
        *,
        attempt=None,
        collection=None,
        cursor=None,
        limit=30,
    ):
        return await self._read(
            actor,
            repo,
            kind,
            number_or_id=number_or_id,
            attempt=attempt,
            collection=collection,
            cursor=cursor,
            limit=limit,
        )

    async def _read(
        self,
        actor,
        repo,
        kind,
        *,
        number_or_id=None,
        attempt=None,
        collection=None,
        cursor=None,
        limit=30,
        filters=None,
    ):
        name, key = await self._authorize(actor, repo)
        if kind not in COLLECTIONS or not 1 <= limit <= 100:
            raise AppError("validation_error", 422, "Invalid request")
        filters = {k: v for k, v in (filters or {}).items() if v is not None}
        if set(filters) - {"state", "head_sha", "branch"}:
            raise AppError("validation_error", 422, "Invalid filter")
        async with transaction(self.factory) as db:
            cov = await coverage(db, repo, kind)
            parent, metadata = None, []
            conditions = [Fact.repository_id == repo, Fact.kind == kind]
            if number_or_id is not None:
                identity = Fact.source_id if kind == "ci_run" else Fact.number
                query = select(Fact).where(*conditions, identity == number_or_id)
                if attempt is not None:
                    query = query.where(Fact.payload["attempt"].as_integer() == attempt)
                parent = await db.scalar(
                    query.order_by(
                        Fact.payload["attempt"].as_integer().desc().nulls_last(),
                        Fact.id,
                    ).limit(1)
                )
                if parent is not None:
                    for group in COLLECTIONS[kind]:
                        version = (
                            f"{parent.payload['base_sha']}:{parent.payload['head_sha']}"
                            if kind == "pull" and group == "files"
                            else str(parent.payload["attempt"])
                            if group == "jobs"
                            else ""
                        )
                        stored = await db.scalar(
                            select(FactCollection).where(
                                FactCollection.repository_id == repo,
                                FactCollection.parent_kind == kind,
                                FactCollection.parent_source_id == parent.source_id,
                                FactCollection.collection == group,
                                FactCollection.version_key == version,
                            )
                        )
                        metadata.append(
                            CollectionView(
                                collection=group,
                                version_key=version,
                                complete=stored.complete if stored else False,
                                reason=stored.reason if stored else "not_collected",
                                observed_at=stored.observed_at if stored else None,
                            )
                        )
                if parent is None:
                    result = None
                elif collection is None:
                    result = FactDetail(
                        item=fact_view(parent, name), collections=metadata, coverage=cov
                    )
                else:
                    if collection not in COLLECTIONS[kind]:
                        raise AppError("validation_error", 422, "Invalid collection")
                    child = COLLECTIONS[kind][collection]
                    conditions = [Fact.repository_id == repo, Fact.kind == child]
                    if collection == "attempts":
                        conditions += [Fact.source_id == parent.source_id]
                    else:
                        conditions += [
                            Fact.parent_kind == kind,
                            Fact.parent_source_id == parent.source_id,
                        ]
                    if collection == "files":
                        conditions += [
                            Fact.payload["base_sha"].astext
                            == parent.payload["base_sha"],
                            Fact.payload["head_sha"].astext
                            == parent.payload["head_sha"],
                        ]
                    if collection == "jobs":
                        conditions += [
                            Fact.payload["attempt"].as_integer()
                            == parent.payload["attempt"]
                        ]
            if number_or_id is None or parent is not None and collection:
                for field, value in filters.items():
                    conditions.append(
                        (Fact.state if field == "state" else Fact.payload[field].astext)
                        == value
                    )
                if kind == "ci_run" and number_or_id is None:
                    other = aliased(Fact)
                    conditions.append(
                        Fact.payload["attempt"].as_integer()
                        == select(func.max(other.payload["attempt"].as_integer()))
                        .where(
                            other.repository_id == Fact.repository_id,
                            other.kind == "ci_run",
                            other.source_id == Fact.source_id,
                        )
                        .correlate(Fact)
                        .scalar_subquery()
                    )
                bound = {
                    "repo": str(repo),
                    "kind": kind,
                    "filters": filters,
                    "parent": number_or_id,
                    "collection": collection,
                    "attempt": attempt,
                }
                if parent:
                    bound["version"] = [
                        parent.version_key,
                        parent.observed_at.isoformat(),
                        parent.payload.get("base_sha"),
                        parent.payload.get("head_sha"),
                    ]
                    bound["collection_observed"] = next(
                        m.observed_at.isoformat() if m.observed_at else None
                        for m in metadata
                        if m.collection == collection
                    )
                purpose = "facts:" + json.dumps(
                    bound, sort_keys=True, separators=(",", ":")
                )
                if cursor:
                    position = self.codec.decode_cursor(
                        cursor, actor.id, purpose, limit
                    )
                    try:
                        identity = UUID(position["id"])
                        stamp = (
                            datetime.fromisoformat(position["updated"])
                            if position["updated"]
                            else None
                        )
                        if stamp and stamp.tzinfo is None:
                            raise ValueError
                    except (ValueError, KeyError, TypeError):
                        raise AppError(
                            "invalid_cursor", 400, "Invalid cursor"
                        ) from None
                    conditions.append(
                        or_(
                            Fact.source_updated_at < stamp,
                            Fact.source_updated_at.is_(None),
                            and_(Fact.source_updated_at == stamp, Fact.id < identity),
                        )
                        if stamp
                        else and_(Fact.source_updated_at.is_(None), Fact.id < identity)
                    )
                rows = list(
                    await db.scalars(
                        select(Fact)
                        .where(*conditions)
                        .order_by(
                            Fact.source_updated_at.desc().nulls_last(), Fact.id.desc()
                        )
                        .limit(limit + 1)
                    )
                )
                token = None
                if len(rows) > limit:
                    last = rows[limit - 1]
                    token = self.codec.encode_cursor(
                        actor.id,
                        purpose,
                        {
                            "id": str(last.id),
                            "updated": last.source_updated_at.isoformat()
                            if last.source_updated_at
                            else None,
                        },
                        limit,
                    )
                result = FactPage(
                    items=[
                        fact_view(row, name, parent.number if parent else None)
                        for row in rows[:limit]
                    ],
                    next_cursor=token,
                    coverage=cov,
                    collection=next(
                        (m for m in metadata if m.collection == collection), None
                    ),
                )
        await recheck(self.policy, actor, repo, key)
        if result is None:
            raise AppError("fact_not_found", 404, "Fact not found")
        return result
