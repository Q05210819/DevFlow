"""Durable single-request collector transitions and shared identity helpers."""

from dataclasses import replace

from pydantic import SecretStr

from devflow.errors import AppError
from devflow.facts.normalize import FactContext, normalize_fact
from devflow.facts.schemas import CollectionInput
from devflow.github.data import DataEndpoint, GitHubDataClient
from devflow.sync.handlers import CollectedPage
from devflow.sync.service import SyncWork


def page(work, *, facts=(), collections=(), unavailable=(), complete=False, **changes):
    checkpoint = work.cursor.model_copy(update=changes)
    reason = checkpoint.gap_reason if complete else None
    return CollectedPage(
        list(facts),
        list(collections),
        checkpoint,
        complete,
        "partial" if reason else "complete",
        reason,
        list(unavailable),
    )


def context(work, **values):
    return FactContext(
        repository_id=work.scope.repository_id,
        observed_at=work.observed_at,
        full_name=work.scope.full_name,
        **values,
    )


def fact(work, kind, payload, **values):
    return normalize_fact(kind, payload, context(work, **values))


def collection(work, name, *, reason=None, version=""):
    target = work.cursor.target
    return CollectionInput(
        repository_id=work.scope.repository_id,
        parent_kind=target.kind,
        parent_source_id=target.source_id,
        collection=name,
        version_key=version,
        complete=reason is None,
        reason=reason,
        observed_at=work.observed_at,
    )


def finish_target(work, **values):
    return page(
        work,
        target=None,
        stage="enumerate",
        child_page=1,
        target_started_at=None,
        captured_sha=None,
        issue_id=None,
        recaptures=0,
        file_count=0,
        pending_collections=[],
        latest_attempt=None,
        current_attempt=None,
        run_head=None,
        missing_stage=None,
        **values,
    )


def start_target(work):
    c = work.cursor
    if not c.pending_targets:
        return None
    target = c.pending_targets[0]
    return page(
        work,
        target=target,
        pending_targets=c.pending_targets[1:],
        stage="issue_identity" if target.kind == "pull" else "detail",
        target_started_at=work.observed_at,
    )


async def read(work, client, token, endpoint, **values):
    return await client.read(token, work.scope.full_name, endpoint, **values)


async def collect_step(
    work: SyncWork, client: GitHubDataClient, token: SecretStr
) -> CollectedPage:
    from devflow.sync import actions, issues, pulls

    c = work.cursor
    if c.stage == "done":
        return page(work, complete=True)
    if c.target is None:
        started = start_target(work)
        if started:
            return started
        if c.mode == "targeted":
            return page(work, stage="done", complete=True)
    try:
        if c.stage == "repository_proof":
            response = await read(work, client, token, DataEndpoint.REPOSITORY)
            if response.item["id"] != work.scope.github_repository_id:
                return finish_target(work, gap_reason="version_unavailable")
            return page(work, stage="permission_proof")
        if c.stage == "permission_proof":
            endpoint = {
                "issues": DataEndpoint.ISSUES,
                "pulls": DataEndpoint.PULLS,
                "actions": DataEndpoint.RUNS,
            }[c.stream]
            await read(
                work,
                client,
                token,
                endpoint,
                filters={}
                if c.stream == "actions"
                else {"state": "all", "sort": "updated", "direction": "desc"},
            )
            return page(work, stage="missing_verify")
        if c.stage == "missing_verify" and c.missing_stage == "issue_identity":
            # Issue endpoint absence cannot establish canonical PR absence.
            await read(work, client, token, DataEndpoint.PULL, target=c.target.number)
            return finish_target(work, gap_reason="version_unavailable")
        if c.stage == "missing_verify":
            work = replace(work, cursor=c.model_copy(update={"stage": c.missing_stage}))
        return await {
            "issues": issues.collect_step,
            "pulls": pulls.collect_step,
            "actions": actions.collect_step,
        }[c.stream](work, client, token)
    except AppError as error:
        if error.code == "permission_missing":
            return CollectedPage(
                [],
                [],
                c.model_copy(
                    update={"stage": "done", "gap_reason": "permission_missing"}
                ),
                True,
                "permission_missing",
                "permission_missing",
            )
        if error.code == "resource_not_found":
            if c.stage in {"detail", "issue_identity"}:
                return page(work, stage="repository_proof", missing_stage=c.stage)
            if c.stage == "missing_verify":
                return finish_target(work, unavailable=[c.target])
            if c.target is not None:
                return finish_target(work, gap_reason="version_unavailable")
            return page(
                work, stage="done", complete=True, gap_reason="permission_missing"
            )
        # Transient errors retain the exact checkpoint and the queue's retry policy.
        if error.code == "github_response_too_large":
            return page(work, stage="done", complete=True, gap_reason=error.code)
        raise
    except (ValueError, KeyError, TypeError):
        raise AppError(
            "github_unavailable", 502, "Invalid GitHub fact response"
        ) from None
