"""Time estimates: priors, replaced by measured EWMAs (``estimates`` table) over time.

Keys are hierarchical so that sparse data still helps, e.g. for boot time:
``create:snapshot:gpu-l40sx1-48gb:tor1`` -> ``create:snapshot:gpu-l40sx1-48gb`` -> ``create:snapshot`` -> prior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRIORS = {
    "create:snapshot": 150.0,  # API create -> machine active
    "create:base": 120.0,
    "boot:snapshot": 40.0,  # active -> worker agent healthy
    "boot:base": 300.0,
}
THROUGHPUT_GBPS = {"local": 2.0, "volume": 0.25, "bucket": 0.35, "hf": 0.30}
EWMA_ALPHA = 0.3


@dataclass
class Estimator:
    values: dict[str, float] = field(default_factory=dict)  # from DB

    def _lookup(self, *keys: str, default: float) -> float:
        for k in keys:
            if k in self.values:
                return self.values[k]
        return default

    def t_create(self, size: str, region: str, image_kind: str) -> float:
        base = f"create:{image_kind}"
        return self._lookup(f"{base}:{size}:{region}", f"{base}:{size}", base, default=PRIORS.get(base, 150.0))

    def t_boot(self, size: str, region: str, image_kind: str) -> float:
        base = f"boot:{image_kind}"
        return self._lookup(f"{base}:{size}:{region}", f"{base}:{size}", base, default=PRIORS.get(base, 120.0))

    def throughput_gbps(self, source: str, region: str) -> float:
        return self._lookup(f"thr:{source}:{region}", f"thr:{source}", default=THROUGHPUT_GBPS.get(source, 0.25))

    def run_seconds_per_unit(
        self, model_id: str, quality: str, gpu_class: str | None, prior: float
    ) -> float:
        if gpu_class:
            return self._lookup(f"run:{model_id}:{quality}:{gpu_class}", default=prior)
        return prior

    def model_init(self, model_id: str, gpu_class: str | None, prior: float) -> float:
        return self._lookup(f"init:{model_id}:{gpu_class}", f"init:{model_id}", default=prior)

    @staticmethod
    def ewma(old: float | None, new: float, alpha: float = EWMA_ALPHA) -> float:
        return new if old is None else (1 - alpha) * old + alpha * new
