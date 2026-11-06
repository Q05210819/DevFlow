"""Model-capable Worker lifecycle: OS ownership, one inference slot, drain.

Ownership is acquired before any model file is loaded. A drain that cannot
finish leaves the owner lock held, because releasing it would let another
process load the models while a physical thread may still be using them.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from devflow.config import Settings
from devflow.knowledge.inference import LocalInference, ModelFingerprint, ModelLoader
from devflow.knowledge.owner import ModelOwner

RuntimeStatus = Literal["starting", "ready", "not_ready", "unhealthy", "stopped"]


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    status: RuntimeStatus
    reason: str | None = None
    fingerprint: ModelFingerprint | None = None


class ModelRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        owner: ModelOwner | None = None,
        inference: LocalInference | None = None,
        loader: ModelLoader | None = None,
    ):
        self._settings = settings
        self._owner = owner or ModelOwner(Path(settings.model_lock_path))
        self.inference = inference or LocalInference(settings, loader=loader)
        self._ready = False
        self._unhealthy = False
        self._closed = False
        self._starting = False
        self._owner_busy = False
        self._failure_reason: str | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._ready and not self._unhealthy and not self._closed

    @property
    def unhealthy(self) -> bool:
        return self._unhealthy

    def health(self) -> RuntimeHealth:
        """Explicit readiness state for logs and operators."""

        if self._closed:
            return RuntimeHealth("stopped", "stopped")
        if self._unhealthy:
            reason = self._failure_reason or getattr(
                self.inference, "failure_reason", None
            )
            return RuntimeHealth("unhealthy", reason or "unhealthy")
        if self._ready:
            return RuntimeHealth(
                "ready", None, getattr(self.inference, "fingerprint", None)
            )
        if self._starting:
            return RuntimeHealth("starting", "loading")
        if self._owner_busy:
            return RuntimeHealth("not_ready", "owner_locked")
        return RuntimeHealth("not_ready", "not_started")

    def mark_unhealthy(self, reason: str | None = None) -> None:
        """Terminal: never claim or start again; keep the owner lock."""

        self._unhealthy = True
        self._ready = False
        if reason is not None:
            self._failure_reason = reason
        elif self._failure_reason is None:
            self._failure_reason = getattr(self.inference, "failure_reason", None)
        self.inference.mark_unhealthy()

    async def ensure_ready(self) -> bool:
        if self._closed or self._unhealthy:
            return False
        if self._ready:
            return True
        async with self._lock:
            if self._closed or self._unhealthy:
                return False
            if self._ready:
                return True
            if not self._owner.acquire():
                self._owner_busy = True
                return False
            self._owner_busy = False
            self._starting = True
            try:
                await self.inference.start()
            except asyncio.CancelledError:
                self._unhealthy = True
                self._failure_reason = "startup_cancelled"
                try:
                    await self.inference.drain()
                except BaseException:  # noqa: BLE001, S110 - keep owner held
                    pass
                else:
                    self._owner.close()
                raise
            except Exception:  # noqa: BLE001 - all start failures are terminal
                self._unhealthy = True
                self._failure_reason = (
                    getattr(self.inference, "failure_reason", None)
                    or "model_load_failed"
                )
                try:
                    await self.inference.drain()
                except BaseException:  # noqa: BLE001, S110 - keep owner held
                    pass
                else:
                    self._owner.close()
                return False
            finally:
                self._starting = False
            self._ready = True
            return True

    async def close(self) -> None:
        if self._closed:
            return
        if not self._owner.acquired:
            self._owner.close()
            self._closed = True
            return
        if self._unhealthy:
            # A physical thread may still be alive; releasing ownership now
            # could let a second process load models concurrently.
            return
        await self.inference.drain()
        self._ready = False
        self._owner.close()
        self._closed = True
