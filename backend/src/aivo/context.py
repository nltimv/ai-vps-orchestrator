"""Process-wide application context (dependency container)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aivo.catalog.loader import Catalog
from aivo.config import EnvSettings, RuntimeSettings
from aivo.db.models import Setting, SystemRow
from aivo.events.bus import EventBus
from aivo.providers.base import GpuOffer
from aivo.providers.runpod.api import RunPodAPI
from aivo.storage.keyring import Keyring
from aivo.storage.media_store import FilesystemStore

if TYPE_CHECKING:
    from aivo.controller.controller import Controller
    from aivo.providers.runpod.catalog import GpuCatalog


@dataclass
class OffersCache:
    offers: list[GpuOffer] = field(default_factory=list)
    fetched_at: float = 0.0
    error: str | None = None


@dataclass
class AppContext:
    env: EnvSettings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    keyring: Keyring
    store: FilesystemStore
    catalog: Catalog
    runpod: RunPodAPI  # RunPod Serverless (or the simulated one, AIVO_PROVIDER=fake)
    bus: EventBus
    deployment_id: uuid.UUID = field(default_factory=uuid.uuid4)
    offers: OffersCache = field(default_factory=OffersCache)
    controller: Controller | None = None
    oidc: Any = None  # lazily created OIDCClient (caches discovery + signing keys)
    _rs_cache: tuple[float, RuntimeSettings] | None = None
    _gpus: GpuCatalog | None = None

    @property
    def gpus(self) -> GpuCatalog:
        """RunPod's GPU types (offers) and what creating an endpoint for one needs."""
        if self._gpus is None:
            from aivo.providers.runpod.catalog import GpuCatalog

            self._gpus = GpuCatalog(self.runpod, self.env)
        return self._gpus

    @property
    def worker_api_url(self) -> str:
        """Where RunPod workers reach aivo (only /api/v1/worker/ with a per-job ticket)."""
        url = self.env.worker_api_url or self.env.public_url
        if not url:
            if self.env.provider == "fake":
                return "http://aivo.internal"
            raise RuntimeError("Set AIVO_PUBLIC_URL (or AIVO_WORKER_API_URL): RunPod workers deliver results there")
        return url.rstrip("/")

    async def load_deployment_id(self) -> uuid.UUID:
        async with self.sessionmaker() as s, s.begin():
            row = (await s.execute(select(SystemRow).where(SystemRow.id == 1))).scalar_one_or_none()
            if row is None:
                row = SystemRow(id=1, deployment_id=uuid.uuid4(), catalog_hash=self.catalog.hash)
                s.add(row)
            else:
                row.catalog_hash = self.catalog.hash
            self.deployment_id = row.deployment_id
        return self.deployment_id

    async def runtime(self, session: AsyncSession | None = None, *, fresh: bool = False) -> RuntimeSettings:
        now = time.monotonic()
        if not fresh and self._rs_cache and now - self._rs_cache[0] < 5:
            return self._rs_cache[1]
        if session is None:
            async with self.sessionmaker() as s:
                rs = await load_runtime(s)
        else:
            rs = await load_runtime(session)
        self._rs_cache = (now, rs)
        return rs

    def invalidate_runtime(self) -> None:
        self._rs_cache = None

    def enabled_models(self, rs: RuntimeSettings) -> set[str]:
        if rs.enabled_models is None:
            return self.catalog.default_enabled()
        return {m for m in rs.enabled_models if m in self.catalog.models}


async def load_runtime(session: AsyncSession) -> RuntimeSettings:
    rows = (await session.execute(select(Setting))).scalars().all()
    data = {r.key: r.value for r in rows}
    defaults = RuntimeSettings().model_dump()
    merged = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
    return RuntimeSettings.model_validate(merged)
