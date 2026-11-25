"""Region names for display (RunPod: one region group, e.g. "eu", spanning the allowed data centers)."""

from __future__ import annotations

from aivo.providers.base import RegionMeta

_REGIONS: dict[str, RegionMeta] = {
    "eu": RegionMeta("eu", "Europe", 50.1, 8.7),
    "us": RegionMeta("us", "United States", 39.0, -95.0),
    "ca": RegionMeta("ca", "Canada", 45.5, -73.6),
}


def register_regions(regions: dict[str, RegionMeta]) -> None:
    _REGIONS.update(regions)


def region_meta(slug: str) -> RegionMeta:
    return _REGIONS.get(slug) or RegionMeta(slug, slug.upper(), 50.1, 8.7)
