#!/usr/bin/env python3
"""Real GPU smoke test on DigitalOcean, with a hard cost cap.

Creates ONE GPU droplet (best-ranked available offer, falling back on capacity
errors), boots the worker, downloads the model, generates one output, downloads
it, then destroys the droplet. Prints a phase-timing report (useful for tuning
cold-start estimates).

Safety:
* refuses to run without --i-understand-this-costs-money (except --dry-run)
* computes a deadline from --max-usd and the machine's hourly price
* a detached watchdog process deletes everything with this run's tag at the
  deadline, even if this process hangs or is killed
* always destroys in `finally`, then verifies nothing with the tag remains

Usage:
  AIVO_DO_TOKEN=... uv run python scripts/do_smoke.py --dry-run
  AIVO_DO_TOKEN=... uv run python scripts/do_smoke.py --max-usd 1.5 --i-understand-this-costs-money
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-usd", type=float, default=1.5)
    ap.add_argument("--model", default="z-image-turbo")
    ap.add_argument("--quality", default="draft")
    ap.add_argument("--prompt", default="a red fox sitting in fresh snow, golden hour")
    ap.add_argument("--region", default=None, help="restrict to one region")
    ap.add_argument("--dry-run", action="store_true", help="only list and rank offers ($0)")
    ap.add_argument("--i-understand-this-costs-money", dest="ok", action="store_true")
    ap.add_argument("--report", default=None, help="write a JSON report here")
    ap.add_argument("--out", default="smoke-output", help="directory for the generated file(s)")
    # internal: watchdog mode
    ap.add_argument("--sweep-tag", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--sweep-at", type=float, default=None, help=argparse.SUPPRESS)
    return ap.parse_args()


def env():  # type: ignore[no-untyped-def]
    from aivo.config import EnvSettings

    e = EnvSettings(provider="digitalocean", media_keks="unused", catalog_dir=ROOT / "catalog")
    if e.do_token is None:
        sys.exit("AIVO_DO_TOKEN is required")
    return e


async def sweep(tag: str, at: float) -> None:
    """Watchdog mode: wait until `at`, then delete every droplet carrying `tag`."""
    from aivo.providers.digitalocean.provider import DigitalOceanProvider

    await asyncio.sleep(max(0, at - time.time()))
    p = DigitalOceanProvider(env())
    try:
        for pi in await p.list_instances(tag):
            print(f"[watchdog] deadline reached: deleting {pi.provider_id}", flush=True)
            await p.destroy_instance(pi.provider_id)
    finally:
        await p.aclose()


def spawn_watchdog(tag: str, at: float) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, __file__, "--sweep-tag", tag, "--sweep-at", str(at)],
        start_new_session=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


async def main(a: argparse.Namespace) -> int:
    from aivo.catalog.loader import Catalog
    from aivo.catalog.presets import FriendlyParams, plan, render_workflow, workflow_params
    from aivo.placement.request import build_request
    from aivo.placement.scorer import Context, rank
    from aivo.providers.base import (
        CapacityError,
        GpuOffer,
        InstanceSpec,
        ProviderError,
        VolumeBusy,
        tags_for,
    )
    from aivo.providers.digitalocean.provider import DigitalOceanProvider
    from aivo.worker_client.client import WorkerClient
    from aivo.worker_client.tls import make_worker_credentials
    from aivo_protocol.worker_api import EnsureFilesIn, FileFetch, FileSource, JobIn, WorkerBootstrap

    e = env()
    cat = Catalog.load(ROOT / "catalog")
    fp = FriendlyParams(kind=cat.get(a.model).manifest.kind, prompt=a.prompt, model_id=a.model, quality=a.quality)
    pl = plan(cat, fp, set(cat.models))
    req = build_request(pl, "balanced")
    p = DigitalOceanProvider(e)
    report: dict = {"model": a.model, "phases": {}, "attempts": []}
    t_start = time.time()
    try:
        offers = await p.list_offers()
        if a.region:
            offers = [o for o in offers if o.region == a.region]
        ctx = Context(gpu_perf={g.id: g.perf for g in cat.gpu_classes.values()}, max_price_hourly=50)
        cands = [c for c in rank(req, offers, [], ctx) if c.instance_id is None]
        print(f"{len(offers)} GPU offers; {len(cands)} suitable for {a.model}:")
        for c in cands[:10]:
            print(
                f"  {c.suitability:3d}  {c.offer.size_slug:22} {c.offer.region:5} ${c.offer.price_hourly:.2f}/h "
                f"est ${c.cost_usd:.3f} ~{c.wait_s / 60:.0f} min  {', '.join(c.reasons)}"
            )
        quota = await p.gpu_quota()
        print(f"account droplet limit: {quota}")
        if a.dry_run or not cands:
            return 0 if cands else 2
        if not a.ok:
            sys.exit("refusing to spend money without --i-understand-this-costs-money")

        run_tag = f"aivo-dep-smoke-{secrets.token_hex(4)}"
        iid = uuid.uuid4()
        top_price = max(c.offer.price_hourly for c in cands[:5])
        budget_s = a.max_usd / top_price * 3600 * 0.8
        if budget_s < 900:
            sys.exit(f"--max-usd {a.max_usd} only buys {budget_s / 60:.0f} min on these machines; raise it")
        deadline = time.time() + budget_s
        watchdog = spawn_watchdog(run_tag, deadline)
        print(f"run tag {run_tag}; hard deadline in {budget_s / 60:.0f} min (watchdog pid {watchdog.pid})")

        creds = make_worker_credentials(iid)
        token = secrets.token_urlsafe(32)
        boot = WorkerBootstrap(
            instance_id=str(iid),
            deployment_id=run_tag,
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            tls_cert_pem=creds.cert_pem,
            tls_key_pem=creds.key_pem,
            worker_image=e.worker_image,
        )
        pi = None
        offer: GpuOffer | None = None
        for c in cands[:8]:
            image = await p.boot_image(c.offer, "base")
            spec = InstanceSpec(
                instance_id=iid,
                name=f"aivo-smoke-{str(iid)[:8]}",
                offer=c.offer,
                image=image,
                bootstrap=boot,
                tags=[*tags_for(run_tag[len("aivo-dep-") :], iid), run_tag],
            )
            t0 = time.time()
            try:
                pi = await p.create_instance(spec)
                offer = c.offer
                report["attempts"].append({"offer": c.offer.key, "outcome": "created"})
                report["phases"]["create_api_s"] = round(time.time() - t0, 1)
                break
            except (CapacityError, VolumeBusy) as err:
                report["attempts"].append({"offer": c.offer.key, "outcome": "capacity", "error": str(err)})
                print(f"  sold out: {c.offer.key}")
        if pi is None or offer is None:
            print("no capacity anywhere right now")
            return 3
        print(f"created droplet {pi.provider_id}: {offer.gpu_model} in {offer.region} (${offer.price_hourly}/h)")
        report["machine"] = {"size": offer.size_slug, "region": offer.region, "price_hourly": offer.price_hourly}
        try:
            t_created = time.time()
            while True:
                cur = await p.get_instance(pi.provider_id)
                if cur and cur.public_ipv4 and cur.status == "active":
                    break
                if time.time() > deadline:
                    raise TimeoutError("no IP before deadline")
                await asyncio.sleep(5)
            report["phases"]["to_active_s"] = round(time.time() - t_created, 1)
            print(f"active at {cur.public_ipv4} after {report['phases']['to_active_s']} s")
            client = WorkerClient(cur.public_ipv4, e.worker_port, creds.ca_pem, creds.sni_name, token)
            t_active = time.time()
            last_err: Exception | None = None
            while True:
                try:
                    h = await client.health()
                    if h.engine_ready:
                        break
                except Exception as err:  # expected while the machine boots
                    last_err = err
                if time.time() > deadline:
                    raise TimeoutError(f"worker never became healthy: {last_err}")
                await asyncio.sleep(5)
            report["phases"]["boot_to_healthy_s"] = round(time.time() - t_active, 1)
            report["gpu"] = h.gpu.model_dump() if h.gpu else None
            print(f"worker healthy after {report['phases']['boot_to_healthy_s']} s on {h.gpu}")

            mf = pl.model.manifest
            variant = next(
                v for v in sorted(mf.variants, key=lambda v: v.min_vram_gb) if v.min_vram_gb <= offer.vram_gb
            )
            files = mf.files_for(variant, pl.mode)
            fetches = [
                FileFetch(
                    dest=f.dest,
                    bytes=f.bytes if f.sha256 else None,
                    sha256=f.sha256,
                    sources=[
                        FileSource(kind="hf", url=f"https://huggingface.co/{f.repo}/resolve/{f.revision}/{f.path}")
                    ],
                )
                for f in files
            ]
            t_files = time.time()
            await client.ensure_files(EnsureFilesIn(files=fetches))
            while True:
                st = await client.files_status([f.dest for f in fetches])
                if st.all_ready:
                    break
                if st.failed:
                    raise RuntimeError(f"model download failed: {[f.error for f in st.failed]}")
                if time.time() > deadline:
                    raise TimeoutError("model download too slow")
                await asyncio.sleep(5)
            gb = sum(f.bytes for f in files) / 1e9
            secs = time.time() - t_files
            report["phases"]["model_download_s"] = round(secs, 1)
            report["phases"]["model_download_gbps"] = round(gb / max(secs, 0.1), 3)
            print(f"model files ready: {gb:.1f} GB in {secs:.0f} s")

            params = workflow_params(cat, pl, fp, variant, seed=42, prefix="smoke")
            wf = render_workflow(pl.model.workflows[pl.mode], params)
            t_run = time.time()
            await client.submit(
                JobIn(
                    job_id="smoke",
                    model_id=a.model,
                    workflow=wf,
                    output=pl.output,
                    required_files=[f.dest for f in files],
                )
            )
            while True:
                js = await client.job("smoke", wait=20)
                if js and js.state.terminal:
                    break
                if time.time() > deadline:
                    raise TimeoutError("generation too slow")
            report["phases"]["generate_s"] = round(time.time() - t_run, 1)
            if js.state != "succeeded":
                raise RuntimeError(f"generation failed: {js.error_code} {js.error}")
            out_dir = Path(a.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            for o in js.outputs:
                await client.download("smoke", o.name, out_dir / o.name)
                print(f"saved {out_dir / o.name} ({o.bytes} bytes)")
            await client.delete_job("smoke")
            h2 = await client.health()
            assert h2.outputs_pending == 0, "worker kept outputs after delete"
            report["phases"]["total_s"] = round(time.time() - t_start, 1)
            await client.aclose()
        finally:
            await p.destroy_instance(pi.provider_id)
            for _ in range(30):
                if not await p.list_instances(run_tag):
                    break
                await asyncio.sleep(5)
            left = await p.list_instances(run_tag)
            report["leftover_droplets"] = [x.provider_id for x in left]
            billed_h = max(300, time.time() - t_created) / 3600
            report["estimated_cost_usd"] = round(billed_h * offer.price_hourly, 3)
            os.killpg(watchdog.pid, signal.SIGTERM) if watchdog.poll() is None else None
            print(
                f"destroyed; leftovers: {report['leftover_droplets'] or 'none'}; "
                f"estimated cost ${report['estimated_cost_usd']}"
            )
            if left:
                print("WARNING: droplets with this run's tag still exist: delete them in the DO console!")
        return 0
    except ProviderError as err:
        print(f"provider error: {err}")
        return 4
    finally:
        if a.report:
            Path(a.report).write_text(json.dumps(report, indent=2))
        await p.aclose()


if __name__ == "__main__":
    args = parse()
    sys.path.insert(0, str(ROOT / "backend" / "src"))
    if args.sweep_tag:
        asyncio.run(sweep(args.sweep_tag, args.sweep_at or time.time()))
        sys.exit(0)
    sys.exit(asyncio.run(main(args)))
