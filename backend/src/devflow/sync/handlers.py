"""One serial stream of bounded HTTP steps; collectors never own transactions."""

from dataclasses import dataclass

from devflow.errors import AppError
from devflow.facts.schemas import CollectionInput, FactInput
from devflow.sync.schemas import SyncCheckpoint
from devflow.sync.service import LeaseLost, UnitComplete


@dataclass(frozen=True, slots=True)
class CollectedPage:
    facts: list[FactInput]
    collections: list[CollectionInput]
    checkpoint: SyncCheckpoint
    complete: bool


async def run_sync(lease, service, client, tokens, *, collector=None):
    if collector is None:
        raise AppError("service_unavailable", 503, "Repository collector unavailable")
    try:
        for _ in range(100):
            work = await service.snapshot(lease)

            async def reserve(snapshot=work):
                await service.reserve_request(lease, snapshot)

            class BudgetClient:
                async def read(self, *args, **kwargs):
                    await reserve()
                    return await client.read(*args, **kwargs)

            permission = {
                "issues": "issues",
                "pulls": "pull_requests",
                "actions": "actions",
            }[work.cursor.stream]
            try:
                token = await tokens.get(
                    work.scope.installation_id,
                    work.scope.github_repository_id,
                    {"metadata": "read", permission: "read"},
                    auth_version=(
                        work.scope.installation_version,
                        work.scope.repository_version,
                    ),
                    before_request=reserve,
                )
                page = await collector(work, BudgetClient(), token)
            except UnitComplete:
                await service.continue_job(lease, work)
                return
            if not await service.commit_page(
                lease,
                work,
                facts=page.facts,
                collections=page.collections,
                checkpoint=page.checkpoint,
                complete=page.complete,
            ):
                return
            if page.checkpoint == work.cursor and not page.complete:
                # An accidental zero-request no-op cannot spin forever.
                raise AppError("internal_error", 500, "Collector made no progress")
        await service.continue_job(lease, work)
    except LeaseLost:
        return
