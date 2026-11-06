"""Real finite chat handlers; the Job transition belongs to the handler.

Task 4 ships only the terminal-thread cleanup entry. The run handler is added
by Task 5, so a ``chat.run`` Job still has no executable handler here.
"""

from devflow.chat.retention import ChatRetention


class ChatCleanupHandler:
    """Drive one bounded terminal-thread sweep under its own lease."""

    def __init__(self, retention: ChatRetention):
        self.retention = retention

    async def run(self, lease) -> None:
        await self.retention.sweep(lease)
