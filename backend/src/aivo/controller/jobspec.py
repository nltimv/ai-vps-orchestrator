"""From a job to what a worker runs: the resolved parameters and the ``WorkSpec`` the worker fetches."""

from __future__ import annotations

import logging
import random
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.catalog.presets import FriendlyParams, Plan, plan, render_workflow, workflow_params
from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.db.models import Asset, Job
from aivo.placement.request import build_request
from aivo.placement.scorer import Request
from aivo_protocol.catalog_types import FileSpec
from aivo_protocol.worker_api import FileFetch, FileSource, OutputSpec, WorkSpec

log = logging.getLogger(__name__)

FRIENDLY_ERRORS = {
    "oom": "The GPU ran out of memory for this request.",
    "timeout": "Creating this took too long and was stopped.",
    "missing_files": "The AI model could not be downloaded to the GPU.",
    "engine": "Something went wrong while creating this.",
    "lost": "The GPU worker stopped responding.",
    "bad_request": "The GPU worker could not understand this request.",
}


def hf_url(f: FileSpec) -> str:
    return f"https://huggingface.co/{f.repo}/resolve/{f.revision}/{f.path}"


def friendly_from_job(app: AppContext, job: Job) -> FriendlyParams:
    kr = app.keyring
    return FriendlyParams.model_validate(
        {
            **job.friendly_params,
            "prompt": kr.open(job.prompt_enc, job.id.bytes) or "",
            "avoid": kr.open(job.avoid_enc, job.id.bytes),
            "lyrics": kr.open(job.lyrics_enc, job.id.bytes),
        }
    )


def job_request(app: AppContext, job: Job, rs: RuntimeSettings) -> tuple[FriendlyParams, Plan, Request]:
    fp = friendly_from_job(app, job)
    pl = plan(app.catalog, fp, app.enabled_models(rs) | {job.model_id})
    req = build_request(pl, job.preference, min_vram_override=job.min_vram_gb, hf_token=app.env.hf_token is not None)
    return fp, pl, req


async def resolve(
    app: AppContext, s: AsyncSession, job: Job, fp: FriendlyParams, pl: Plan, variant_id: str, rs: RuntimeSettings
) -> dict[str, Any]:
    """Everything the worker needs, fixed at submission (the seed too, so a retry makes the same picture)."""
    variant = pl.model.manifest.variant(variant_id)
    prefix = f"aivo_{job.id.hex[:10]}"
    old_seed = (job.resolved or {}).get("seed")
    params = workflow_params(
        app.catalog, pl, fp, variant, seed=old_seed if old_seed is not None else random.randrange(2**32), prefix=prefix
    )
    inputs: list[dict[str, Any]] = []
    for aid in job.input_asset_ids or []:
        a = (await s.execute(select(Asset).where(Asset.id == aid))).scalar_one_or_none()
        if a is None:
            continue
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(a.media_type, ".png")
        name = f"ref-{job.id.hex[:12]}{ext}"
        inputs.append({"name": name, "asset_id": str(a.id), "storage_path": a.storage_path})
        params["reference_image"] = name
    return {
        "variant_id": variant_id,
        "mode": pl.mode,
        "quality": pl.quality,
        "params": params,
        "output": pl.output.model_dump(),
        "units": pl.units,
        "inputs": inputs,
        "seed": params.get("seed"),
        "timeout_s": rs.job_timeout_s,
    }


def _hf_source_url(app: AppContext, f: FileSpec) -> str:
    url = hf_url(f)
    if f.gated and app.env.hf_token:
        # resolve to a short-lived signed CDN link so the worker never holds the Hugging Face token
        try:
            r = httpx.get(
                url,
                headers={"Authorization": f"Bearer {app.env.hf_token.get_secret_value()}", "Range": "bytes=0-0"},
                follow_redirects=False,
                timeout=20,
            )
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                return r.headers["location"]
        except httpx.HTTPError as e:
            log.warning("could not resolve gated download: %s", e)
    return url


def work_spec(app: AppContext, job: Job) -> WorkSpec:
    resolved = job.resolved or {}
    cm = app.catalog.get(job.model_id)
    mf = cm.manifest
    variant = mf.variant(resolved["variant_id"])
    files = mf.files_for(variant, resolved["mode"])
    return WorkSpec(
        job_id=str(job.id),
        model_id=job.model_id,
        workflow=render_workflow(cm.workflows[resolved["mode"]], resolved["params"]),
        files=[
            FileFetch(
                dest=f.dest,
                bytes=f.bytes if f.sha256 else None,
                sha256=f.sha256,
                sources=[FileSource(kind="hf", url=_hf_source_url(app, f))],
            )
            for f in files
        ],
        inputs=[i["name"] for i in resolved.get("inputs", [])],
        output=OutputSpec.model_validate(resolved["output"]),
        timeout_s=int(resolved.get("timeout_s", 3600)),
    )


def job_files(app: AppContext, job: Job) -> list[FileSpec]:
    try:
        resolved = job.resolved or {}
        mf = app.catalog.get(job.model_id).manifest
        return list(mf.files_for(mf.variant(resolved["variant_id"]), resolved["mode"]))
    except Exception:
        return []


def download_error(message: str, files: list[FileSpec]) -> str:
    """A job-facing message for model files that couldn't be downloaded.

    ``message`` is the worker's report: ``"<dest>: <error>; <dest>: <error>"``.
    """
    refused = [part.split(":", 1)[0].strip() for part in message.split(";") if "HTTP 401" in part or "HTTP 403" in part]
    detail = message[:300]
    if refused:
        gated = {f.dest for f in files if f.gated}
        if any(d in gated for d in refused):
            return (
                "This engine's model files need a Hugging Face token: accept the model's license on "
                f"huggingface.co and set AIVO_HF_TOKEN. ({detail})"
            )
        return f"Hugging Face refused to download this engine's model files (moved, or now gated). ({detail})"
    return f"The AI model's files couldn't be downloaded. ({detail})"
