"""FastAPI application factory. ``role`` selects api / controller / all."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from aivo import __version__
from aivo.api.routes import assets, auth, catalog, events, health, jobs, machines, oidc, settings, spending, worker
from aivo.auth.passwords import hash_password
from aivo.catalog.loader import Catalog
from aivo.config import EnvSettings, get_env
from aivo.context import AppContext
from aivo.db.models import User
from aivo.db.session import make_engine, make_sessionmaker
from aivo.events.bus import EventBus
from aivo.providers.runpod.api import HttpRunPod, RunPodAPI
from aivo.storage.keyring import Keyring
from aivo.storage.media_store import FilesystemStore

log = logging.getLogger("aivo")

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def make_runpod(env: EnvSettings) -> RunPodAPI:
    if env.provider == "fake":
        from aivo.providers.runpod.fake import FakeRunPod

        return FakeRunPod(
            boot_seconds=env.fake_boot_seconds, job_seconds=env.fake_job_seconds, chaos=list(env.fake_chaos)
        )
    if env.runpod_api_key is None:
        raise ValueError("AIVO_RUNPOD_API_KEY is required (or AIVO_PROVIDER=fake for a simulated RunPod)")
    return HttpRunPod(
        env.runpod_api_key.get_secret_value(), api_base=env.runpod_api_base, queue_base=env.runpod_queue_base
    )


def build_context(env: EnvSettings, runpod: RunPodAPI | None = None) -> AppContext:
    engine = make_engine(env.database_url)
    keyring = Keyring.parse(env.media_keks.get_secret_value(), env.media_active_key_id or None)
    env.scratch_dir.mkdir(parents=True, exist_ok=True)
    return AppContext(
        env=env,
        engine=engine,
        sessionmaker=make_sessionmaker(engine),
        keyring=keyring,
        store=FilesystemStore(env.media_dir, keyring),
        catalog=Catalog.load(env.catalog_dir),
        runpod=runpod or make_runpod(env),
        bus=EventBus(env.database_url),
    )


async def bootstrap_admin(ctx: AppContext) -> None:
    env = ctx.env
    if not (env.admin_email and env.admin_password):
        return
    async with ctx.sessionmaker() as s, s.begin():
        n = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        if n == 0:
            s.add(
                User(
                    email=env.admin_email.lower(),
                    display_name="Admin",
                    password_hash=hash_password(env.admin_password.get_secret_value()),
                    role="admin",
                )
            )
            log.info("created initial admin %s", env.admin_email)


def create_app(
    env: EnvSettings | None = None,
    *,
    ctx: AppContext | None = None,
    role: str | None = None,
    controller_kwargs: dict | None = None,
) -> FastAPI:  # type: ignore[type-arg]
    env = env or (ctx.env if ctx else get_env())
    role = role or env.role

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = ctx or build_context(env)
        app.state.ctx = c
        if hasattr(c.runpod, "attach"):  # the simulated RunPod's workers call this app in-process
            import httpx

            c.runpod.attach(httpx.ASGITransport(app=app))
        await c.load_deployment_id()
        await bootstrap_admin(c)
        await c.bus.start()
        controller = None
        wake_task = None
        if role in ("controller", "all"):
            from aivo.controller.controller import Controller

            controller = Controller(c, **(controller_kwargs or {}))
            await controller.start()

            async def wake_on_events() -> None:
                q = c.bus.subscribe()
                while True:
                    await q.get()
                    controller.wake()

            wake_task = asyncio.create_task(wake_on_events())
        log.info(
            "aivo %s started (role=%s, provider=%s, deployment=%s)", __version__, role, c.runpod.name, c.deployment_id
        )
        try:
            yield
        finally:
            if wake_task:
                wake_task.cancel()
            if controller:
                await controller.stop()
            await c.bus.stop()
            with contextlib.suppress(Exception):
                await c.runpod.aclose()
            await c.engine.dispose()

    app = FastAPI(
        title="aivo",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    if env.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=env.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        # A custom header can't be sent cross-site without a CORS preflight, which we don't allow.
        if (
            request.method in UNSAFE
            and request.url.path.startswith("/api/")
            and request.headers.get("x-requested-with") != "aivo"
        ):
            return JSONResponse({"detail": "Missing X-Requested-With header"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault("referrer-policy", "same-origin")
        return response

    for r in (
        auth.router,
        auth.admin_router,
        oidc.router,
        catalog.router,
        jobs.router,
        assets.router,
        machines.router,
        spending.router,
        settings.router,
        events.router,
        health.router,
        worker.router,
    ):
        app.include_router(r, prefix="/api/v1")
    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=get_env().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
