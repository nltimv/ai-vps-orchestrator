"""Load and validate the model catalog (``catalog/``)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from aivo_protocol.catalog_types import MODE_NEEDS_REFERENCE, QUALITIES, ModelManifest, Rewriter


class CatalogError(Exception):
    pass


class GpuClass(BaseModel):
    id: str
    label: str
    vendor: str = "nvidia"
    perf: float  # relative throughput vs RTX 4000 Ada = 1.0
    aliases: list[str] = Field(default_factory=list)


class Style(BaseModel):
    id: str
    label: str
    suffix: str = ""
    family_suffix: dict[str, str] = Field(default_factory=dict)

    def suffix_for(self, family: str) -> str:
        return self.family_suffix.get(family, self.suffix)


@dataclass
class CatalogModel:
    manifest: ModelManifest
    dir: Path
    workflows: dict[str, dict[str, Any]]

    @property
    def id(self) -> str:
        return self.manifest.id


@dataclass
class Catalog:
    models: dict[str, CatalogModel]
    routing: dict[str, dict[str, dict[str, list[str]]]]
    styles: dict[str, Style]
    gpu_classes: dict[str, GpuClass]
    hash: str
    rewriter: Rewriter | None = None
    rewriter_workflow: dict[str, Any] = field(default_factory=dict)
    _alias_index: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for gc in self.gpu_classes.values():
            for a in [gc.id, *gc.aliases]:
                self._alias_index[a.lower()] = gc.id

    @classmethod
    def load(cls, root: Path) -> Catalog:
        root = Path(root)
        if not root.is_dir():
            raise CatalogError(f"catalog directory not found: {root}")
        h = hashlib.sha256()
        models: dict[str, CatalogModel] = {}
        for mf in sorted((root / "models").glob("*/manifest.yaml")):
            raw = mf.read_bytes()
            h.update(raw)
            try:
                manifest = ModelManifest.model_validate(yaml.safe_load(raw))
            except Exception as e:
                raise CatalogError(f"{mf}: {e}") from e
            if manifest.id in models:
                raise CatalogError(f"duplicate model id {manifest.id}")
            workflows: dict[str, dict[str, Any]] = {}
            for m in manifest.modes:
                wf_path = (mf.parent / m.workflow).resolve()
                if mf.parent.resolve() not in wf_path.parents:
                    raise CatalogError(f"{manifest.id}: workflow path escapes model dir")
                if not wf_path.exists():
                    raise CatalogError(f"{manifest.id}: missing workflow {m.workflow}")
                wraw = wf_path.read_bytes()
                h.update(wraw)
                workflows[m.mode] = json.loads(wraw)
            models[manifest.id] = CatalogModel(manifest=manifest, dir=mf.parent, workflows=workflows)

        def _yaml(name: str) -> Any:
            p = root / name
            if not p.exists():
                return None
            raw = p.read_bytes()
            h.update(raw)
            return yaml.safe_load(raw)

        routing = _yaml("routing.yaml") or {}
        styles = {s["id"]: Style.model_validate(s) for s in (_yaml("styles.yaml") or [])}
        gpu_classes = {g["id"]: GpuClass.model_validate(g) for g in (_yaml("gpu_classes.yaml") or [])}
        rewriter, rewriter_workflow = None, {}
        if (raw_rw := _yaml("rewriting.yaml")) is not None:
            try:
                rewriter = Rewriter.model_validate(raw_rw)
            except Exception as e:
                raise CatalogError(f"rewriting.yaml: {e}") from e
            wf_path = (root / rewriter.workflow).resolve()
            if root.resolve() not in wf_path.parents or not wf_path.exists():
                raise CatalogError(f"rewriting.yaml: missing workflow {rewriter.workflow}")
            wraw = wf_path.read_bytes()
            h.update(wraw)
            rewriter_workflow = json.loads(wraw)
        cat = cls(
            models=models,
            routing=routing,
            styles=styles,
            gpu_classes=gpu_classes,
            hash=h.hexdigest(),
            rewriter=rewriter,
            rewriter_workflow=rewriter_workflow,
        )
        cat.validate()
        return cat

    def validate(self) -> None:
        for kind, modes in self.routing.items():
            for mode, qualities in modes.items():
                for q, ids in qualities.items():
                    if q not in QUALITIES:
                        raise CatalogError(f"routing {kind}/{mode}: unknown quality {q!r}")
                    for mid in ids:
                        m = self.models.get(mid)
                        if m is None:
                            raise CatalogError(f"routing {kind}/{mode}/{q} references unknown model {mid}")
                        if m.manifest.kind != kind or mode not in m.workflows:
                            raise CatalogError(f"routing {kind}/{mode}/{q}: model {mid} does not support it")
        for m in self.models.values():
            for gc in m.manifest.bench.seconds:
                if self.gpu_classes and gc not in self.gpu_classes:
                    raise CatalogError(f"{m.id}: bench references unknown gpu class {gc}")
            for mode, rid in m.manifest.rewrite.items():
                if self.rewriter is None or rid not in self.rewriter.instructions:
                    raise CatalogError(f"{m.id}: rewrite {mode} -> {rid!r}: no such instructions in rewriting.yaml")
        if self.rewriter is not None:
            from aivo.catalog.presets import placeholders

            if self.rewriter.output_node not in self.rewriter_workflow:
                raise CatalogError(f"rewriting.yaml: output_node {self.rewriter.output_node} isn't in the workflow")
            missing = placeholders(self.rewriter_workflow) - {*self.rewriter.params, "text", "seed"}
            if missing:
                raise CatalogError(f"rewriting.yaml: the workflow's {sorted(missing)} have no value in params")

    # -- lookups ------------------------------------------------------------------

    def gpu_class_for(self, *names: str | None) -> str | None:
        for n in names:
            if n and n.lower() in self._alias_index:
                return self._alias_index[n.lower()]
        return None

    def get(self, model_id: str) -> CatalogModel:
        try:
            return self.models[model_id]
        except KeyError as e:
            raise CatalogError(f"unknown model {model_id}") from e

    def route(self, kind: str, mode: str, quality: str, enabled: set[str]) -> CatalogModel:
        """Pick the model for "Auto": first enabled model listed for kind/mode/quality."""
        by_quality = self.routing.get(kind, {}).get(mode, {})
        # a level without its own routing (e.g. "high" for pictures) routes like standard
        candidates = by_quality.get(quality) or by_quality.get("standard", [])
        for mid in candidates:
            if mid in enabled:
                return self.models[mid]
        # any enabled model that supports the mode
        for m in self.models.values():
            if m.manifest.kind == kind and mode in m.workflows and m.id in enabled:
                return m
        raise CatalogError(f"no enabled model can do {kind}/{mode}")

    def default_enabled(self) -> set[str]:
        return {mid for mid, m in self.models.items() if m.manifest.enabled_by_default}

    def modes_for_kind(self, kind: str) -> list[str]:
        out: list[str] = []
        for m in self.models.values():
            if m.manifest.kind == kind:
                for mode in m.workflows:
                    if mode not in out:
                        out.append(mode)
        return out

    @staticmethod
    def mode_needs_reference(mode: str) -> bool:
        return mode in MODE_NEEDS_REFERENCE
