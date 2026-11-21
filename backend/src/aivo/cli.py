"""``aivo`` command line: serve, migrate, admin bootstrap, janitor, key rotation, inventory."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging

import typer
import uvicorn

from aivo.config import get_env

app = typer.Typer(help="aivo: on-demand GPU orchestration for AI generation", no_args_is_help=True)


@app.command()
def serve(
    role: str = typer.Option(None, help="api | controller | all"),
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Run the HTTP API and/or controller."""
    import os

    if role:
        os.environ["AIVO_ROLE"] = role
        get_env.cache_clear()
    uvicorn.run(
        "aivo.main:app_factory",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    from aivo.db.migrate import upgrade

    upgrade(get_env().database_url)
    typer.echo("database is up to date")


@app.command("create-admin")
def create_admin(email: str, name: str = "Admin") -> None:
    """Create an admin account (prompts for the password)."""
    from sqlalchemy import select

    from aivo.auth.passwords import hash_password
    from aivo.db.models import User
    from aivo.db.session import make_engine, make_sessionmaker

    pw = getpass.getpass("Password (min 10 chars): ")
    if len(pw) < 10:
        raise typer.BadParameter("password too short")

    async def go() -> None:
        eng = make_engine(get_env().database_url)
        async with make_sessionmaker(eng)() as s, s.begin():
            if (await s.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none():
                raise typer.BadParameter("user exists")
            s.add(User(email=email.lower(), display_name=name, password_hash=hash_password(pw), role="admin"))
        await eng.dispose()

    asyncio.run(go())
    typer.echo(f"admin {email} created")


@app.command("generate-key")
def generate_key(key_id: str = "k1") -> None:
    """Print a new media encryption key entry for AIVO_MEDIA_KEKS."""
    from aivo.storage.keyring import Keyring

    typer.echo(Keyring.generate_spec(key_id))


@app.command("rewrap-keys")
def rewrap_keys() -> None:
    """Re-wrap every stored file's key with the active key id (after adding a new key)."""
    from sqlalchemy import update

    from aivo.db.models import Asset
    from aivo.db.session import make_engine, make_sessionmaker
    from aivo.storage.keyring import Keyring
    from aivo.storage.media_store import FilesystemStore

    env = get_env()
    kr = Keyring.parse(env.media_keks.get_secret_value(), env.media_active_key_id or None)
    n = FilesystemStore(env.media_dir, kr).rewrap_all()

    async def mark() -> None:
        eng = make_engine(env.database_url)
        async with make_sessionmaker(eng)() as s, s.begin():
            await s.execute(update(Asset).values(key_id=kr.active_id))
        await eng.dispose()

    asyncio.run(mark())
    typer.echo(f"re-wrapped {n} files to key {kr.active_id}")


@app.command()
def janitor(stale_after_s: int = 120) -> None:
    """Backstop (e.g. a Kubernetes CronJob): if the controller is gone, destroy expired/idle machines."""
    from aivo.controller.lease import lease_status
    from aivo.controller.reconciler import Reconciler
    from aivo.main import build_context

    logging.basicConfig(level="INFO")

    async def go() -> None:
        ctx = build_context(get_env())
        await ctx.load_deployment_id()
        async with ctx.sessionmaker() as s:
            st = await lease_status(s)
        if st and st["alive"] and st["last_tick_age_s"] < stale_after_s:
            typer.echo("controller is alive; nothing to do")
            await ctx.engine.dispose()
            return
        typer.echo("controller looks dead: running a destroy-only reconcile pass")
        r = Reconciler(ctx)
        rs = await ctx.runtime(fresh=True)
        remote = await ctx.provider.list_instances(
            __import__("aivo.providers.base", fromlist=["x"]).deployment_tag(ctx.deployment_id)
        )
        await r._sync(remote)
        await r._idle_and_lifetime(rs)
        await r._drain()
        await r._destroy()
        await r._budget(rs)
        await ctx.provider.aclose()
        await ctx.engine.dispose()

    asyncio.run(go())


@app.command()
def inventory() -> None:
    """List every cloud resource tagged for this deployment (and what it costs)."""
    from aivo.main import build_context
    from aivo.providers.base import deployment_tag

    async def go() -> None:
        ctx = build_context(get_env())
        await ctx.load_deployment_id()
        machines = await ctx.provider.list_instances(deployment_tag(ctx.deployment_id))
        offers = {o.size_slug: o.price_hourly for o in await ctx.provider.list_offers()}
        out = [
            {
                "id": m.provider_id,
                "name": m.name,
                "status": m.status,
                "size": m.size_slug,
                "region": m.region,
                "usd_per_hour": offers.get(m.size_slug or ""),
            }
            for m in machines
        ]
        typer.echo(json.dumps({"deployment": str(ctx.deployment_id), "machines": out}, indent=2))
        await ctx.provider.aclose()
        await ctx.engine.dispose()

    asyncio.run(go())


@app.command("catalog-check")
def catalog_check(
    online: bool = typer.Option(False, help="Also check every model file on Hugging Face (exists, size, gated)"),
) -> None:
    """Validate the model catalog and render every preset combination."""
    from aivo.catalog.loader import Catalog
    from aivo.catalog.presets import FriendlyParams, plan, render_workflow, workflow_params

    cat = Catalog.load(get_env().catalog_dir)
    n = 0
    for m in cat.models.values():
        for mode in m.workflows:
            for q in ("draft", "standard", "best"):
                fp = FriendlyParams(
                    kind=m.manifest.kind,
                    prompt="test",
                    model_id=m.id,
                    quality=q,  # type: ignore[arg-type]
                    mode=mode,
                    reference_asset_id=__import__("uuid").uuid4() if mode in ("edit", "i2v") else None,
                )
                pl = plan(cat, fp, set(cat.models))
                for v in m.manifest.variants:
                    render_workflow(m.workflows[mode], workflow_params(cat, pl, fp, v, seed=1))
                    n += 1
    typer.echo(f"catalog OK: {len(cat.models)} models, {n} renders")
    if online:
        problems = _check_files_online(cat)
        for p in problems:
            typer.echo(f"PROBLEM: {p}", err=True)
        if problems:
            raise typer.Exit(1)
        typer.echo("model files OK on Hugging Face")


def _check_files_online(cat) -> list[str]:  # type: ignore[no-untyped-def]
    import httpx

    from aivo.cache.spaces import hf_url

    token = get_env().hf_token
    seen: dict[str, object] = {}
    for m in cat.models.values():
        for f in m.manifest.files:
            seen.setdefault(hf_url(f), f)
    problems: list[str] = []
    with httpx.Client(timeout=30) as c:
        for url, f in seen.items():
            headers = {"Authorization": f"Bearer {token.get_secret_value()}"} if token and f.gated else {}  # type: ignore[attr-defined]
            size = None
            for _ in range(3):  # follow Hugging Face's own redirects, not the download CDN
                r = c.head(url, headers=headers)
                size = size or r.headers.get("x-linked-size")
                loc = r.headers.get("location", "")
                if r.status_code in (301, 302, 307, 308) and loc.startswith("/"):
                    url = f"https://huggingface.co{loc}"
                    continue
                break
            name = f"{f.repo}/{f.path}"  # type: ignore[attr-defined]
            if r.status_code in (401, 403):
                if not f.gated:  # type: ignore[attr-defined]
                    problems.append(f"{name}: needs a Hugging Face token but isn't marked gated")
                elif token:
                    problems.append(f"{name}: refused with AIVO_HF_TOKEN (license not accepted?)")
                else:
                    typer.echo(f"note: {name} is gated (needs AIVO_HF_TOKEN)")
            elif r.status_code >= 400:
                problems.append(f"{name}: HTTP {r.status_code} {r.headers.get('x-error-message', '')}".strip())
            elif size and f.bytes and abs(int(size) - f.bytes) > 0.05 * f.bytes:  # type: ignore[attr-defined]
                problems.append(f"{name}: {int(size) / 1e9:.2f} GB, catalog says {f.bytes / 1e9:.2f} GB")  # type: ignore[attr-defined]
    return problems


if __name__ == "__main__":
    app()
