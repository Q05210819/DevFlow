"""Real finite knowledge handlers; completion belongs to the handler transaction."""

from typing import Protocol

from devflow.knowledge.service import KnowledgeService


class KnowledgeImportHandler:
    def __init__(self, service: KnowledgeService):
        self.service = service

    async def run(self, lease) -> None:
        await self.service.import_batch(lease)


class IndexBatchHandler:
    """Drive one bounded index batch; publication stays inside the builder."""

    def __init__(self, builder):
        self.builder = builder

    async def run(self, lease) -> None:
        await self.builder.run_batch(lease)


class QueryHandler:
    """Execute one private retrieval query; the service publishes the result."""

    def __init__(self, service):
        self.service = service

    async def run(self, lease) -> None:
        await self.service.run(lease)


class ModelHandler(Protocol):
    async def run(self, lease) -> None: ...


# Registered only by the task that ships the real handler. An empty map makes a
# model Worker fail the Job explicitly instead of pretending success.
MODEL_HANDLERS: dict[str, ModelHandler] = {}
