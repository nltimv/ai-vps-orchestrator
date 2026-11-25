"""From a job to what a worker runs: the resolved parameters and the ``WorkSpec`` the worker fetches."""

from __future__ import annotations

import logging
import random
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.catalog.presets import (
    PROMPT_MARKER,
    TEXT_PARAMS,
    FriendlyParams,
    Plan,
    describe,
    plan,
    render_workflow,
    rewrite_for,
    rewrite_input,
    runs,
    text_params,
    workflow_params,
)
from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.db.models import Asset, Job
from aivo.placement.request import build_request
from aivo.placement.scorer import Request
from aivo.safety import minors_mentioned
from aivo_protocol.catalog_types import FileSpec, Rewriter
from aivo_protocol.worker_api import FileFetch, FileSource, OutputSpec, RewriteSpec, WorkSpec

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
        # the user's words stay sealed: work_spec() adds them back from the job's encrypted fields
        "params": {k: v for k, v in params.items() if k not in TEXT_PARAMS},
        "output": pl.output.model_dump(),
        "units": pl.units,
        "inputs": inputs,
        "seed": params.get("seed"),
        # the time limit is per run (one batch of pictures, one video): a creation of 4 videos gets 4x
        "timeout_s": rs.job_timeout_s * len(runs(pl.model.manifest, pl.output.count)),
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


def _fetch(app: AppContext, f: FileSpec) -> FileFetch:
    return FileFetch(
        dest=f.dest,
        bytes=f.bytes if f.sha256 else None,
        sha256=f.sha256,
        sources=[FileSource(kind="hf", url=_hf_source_url(app, f))],
    )


def work_spec(app: AppContext, job: Job, *, can_rewrite: bool = True, can_batch: bool = True) -> WorkSpec:
    """What the worker runs.

    ``can_rewrite``: the worker knows how to improve the description (protocol 2.1); ``can_batch``: it runs
    further batches (protocol 2.2). Older workers make the first batch only.
    """
    resolved = job.resolved or {}
    cm = app.catalog.get(job.model_id)
    mf = cm.manifest
    mode = resolved["mode"]
    variant = mf.variant(resolved["variant_id"])
    files = list(mf.files_for(variant, mode))
    fp = friendly_from_job(app, job)
    rid = rewrite_for(app.catalog, mf, mode, fp) if can_rewrite else None
    used = app.keyring.open(job.prompt_used_enc, job.id.bytes) if job.prompt_used_enc else None
    rewrite: RewriteSpec | None = None
    if rid and used:  # improved on an earlier attempt: create with the same words again
        text = text_params(app.catalog, mf, fp, prompt=used)
    elif rid:
        rw = app.catalog.rewriter
        assert rw is not None
        text = text_params(app.catalog, mf, fp, prompt=PROMPT_MARKER)
        have = {f.dest for f in files}
        files = [f for f in rw.files if f.dest not in have] + files  # the language model first
        rewrite = RewriteSpec(
            workflow=render_workflow(
                app.catalog.rewriter_workflow,
                {
                    **rw.params,
                    "text": rewrite_input(app.catalog, mf, rid, fp),
                    "seed": int(resolved.get("seed") or 0),
                },
            ),
            output_node=rw.output_node,
            marker=PROMPT_MARKER,
            fallback=describe(app.catalog, mf, fp),
            files=[f.dest for f in rw.files],
        )
    else:
        text = text_params(app.catalog, mf, fp)
    params = {**resolved["params"], **text}
    batches = runs(mf, OutputSpec.model_validate(resolved["output"]).count) if can_batch else [params.get("batch", 1)]
    seed = int(params.get("seed") or 0)
    workflows = [
        render_workflow(cm.workflows[mode], {**params, "batch": n, "seed": (seed + i) % 2**32})
        for i, n in enumerate(batches)
    ]
    return WorkSpec(
        job_id=str(job.id),
        model_id=job.model_id,
        workflow=workflows[0],
        more=workflows[1:],
        files=[_fetch(app, f) for f in files],
        inputs=[i["name"] for i in resolved.get("inputs", [])],
        output=OutputSpec.model_validate(resolved["output"]),
        timeout_s=int(resolved.get("timeout_s", 3600)),
        rewrite=rewrite,
    )


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_SPECIAL = re.compile(r"<\|[a-z_]+\|>|</?think>")
_LABEL = re.compile(r"^\**\s*(?:improved |detailed |final |video |image )?prompt\s*\**\s*:\s*\**\s*", re.IGNORECASE)


_QUOTED = re.compile(r'"([^"]{2,})"|“([^”]{2,})”|(?<!\w)\'([^\']{2,}?)\'(?!\w)')
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _without_invented_text(text: str, original: str) -> str:
    """Drop sentences that put words in quotes the user never wrote: engines like FLUX.2 render them."""
    own = " ".join(original.lower().split())
    keep = []
    for sentence in _SENTENCE.split(text):
        quoted = [next(g for g in m.groups() if g) for m in _QUOTED.finditer(sentence)]
        if any(" ".join(q.lower().split()) not in own for q in quoted):
            continue
        keep.append(sentence)
    return " ".join(keep)


def accept_rewrite(
    rw: Rewriter, text: str | None, *, adult: bool, original: str | None = None
) -> tuple[str | None, str]:
    """The language model's text, tidied, if it can be used; else ``None`` and why not.

    ``original``: the user's description; quoted text that isn't in it is removed.
    """
    if not text:
        return None, "the language model wrote nothing"
    if "<think>" in text and "</think>" not in text:
        return None, "the language model didn't finish"
    t = _THINK.sub("", text)
    t = _SPECIAL.sub("", t).strip()
    t = _LABEL.sub("", t).strip()
    t = " ".join(t.split())  # one paragraph
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'“”":
        t = t[1:-1].strip()
    if t.startswith("“") and t.endswith("”"):
        t = t[1:-1].strip()
    if original is not None:
        t = _without_invented_text(t, original)
    if len(t) < rw.min_chars:
        return None, "the language model's text was too short"
    low = t.lower()
    if any(low.startswith(r) for r in rw.refusals):
        return None, "the language model declined"
    if len(t) > rw.max_chars:
        cut = t[: rw.max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: end + 1] if end > rw.min_chars else cut
    if adult and minors_mentioned(t):
        return None, "the improved text broke the adult content rules"
    return t, ""


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
