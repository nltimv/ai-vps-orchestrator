"""Latency from the backend to each GPU datacenter (TCP connect time, median of 3).

Only regions that actually offer GPUs are probed. Failures are simply skipped:
the scorer then uses a neutral default.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time

from aivo.context import AppContext
from aivo.db.models import RegionProbe

log = logging.getLogger(__name__)


async def tcp_rtt_ms(host: str, port: int = 443, timeout: float = 3.0) -> float | None:
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, TimeoutError):
        return None
    rtt = (time.perf_counter() - t0) * 1000
    w.close()
    try:
        await w.wait_closed()
    except OSError:
        pass
    return rtt


async def probe_regions(app: AppContext, regions: set[str]) -> dict[str, float]:
    metas = app.provider.regions()
    out: dict[str, float] = {}
    for r in sorted(regions):
        meta = metas.get(r)
        if not meta or not meta.probe_host:
            continue
        samples = [x for x in [await tcp_rtt_ms(meta.probe_host) for _ in range(3)] if x is not None]
        if samples:
            out[r] = statistics.median(samples)
    if out:
        async with app.sessionmaker() as s, s.begin():
            for r, ms in out.items():
                s.add(RegionProbe(provider=app.provider.name, region=r, rtt_ms=ms, method="tcp"))
    return out
