"""Static metadata for DigitalOcean datacenters (coordinates for latency estimates), plus other providers' regions."""

from __future__ import annotations

from aivo.providers.base import RegionMeta

DO_REGIONS: dict[str, RegionMeta] = {
    r.slug: r
    for r in [
        RegionMeta("nyc1", "New York 1", 40.71, -74.01, "speedtest-nyc1.digitalocean.com"),
        RegionMeta("nyc2", "New York 2", 40.71, -74.01, "speedtest-nyc2.digitalocean.com"),
        RegionMeta("nyc3", "New York 3", 40.71, -74.01, "speedtest-nyc3.digitalocean.com"),
        RegionMeta("tor1", "Toronto", 43.65, -79.38, "speedtest-tor1.digitalocean.com"),
        RegionMeta("atl1", "Atlanta", 33.75, -84.39, "speedtest-atl1.digitalocean.com"),
        RegionMeta("ric1", "Richmond", 37.54, -77.44, "speedtest-ric1.digitalocean.com"),
        RegionMeta("sfo2", "San Francisco 2", 37.77, -122.42, "speedtest-sfo2.digitalocean.com"),
        RegionMeta("sfo3", "San Francisco 3", 37.77, -122.42, "speedtest-sfo3.digitalocean.com"),
        RegionMeta("ams3", "Amsterdam", 52.37, 4.90, "speedtest-ams3.digitalocean.com"),
        RegionMeta("lon1", "London", 51.51, -0.13, "speedtest-lon1.digitalocean.com"),
        RegionMeta("fra1", "Frankfurt", 50.11, 8.68, "speedtest-fra1.digitalocean.com"),
        RegionMeta("sgp1", "Singapore", 1.35, 103.82, "speedtest-sgp1.digitalocean.com"),
        RegionMeta("blr1", "Bangalore", 12.97, 77.59, "speedtest-blr1.digitalocean.com"),
        RegionMeta("syd1", "Sydney", -33.87, 151.21, "speedtest-syd1.digitalocean.com"),
    ]
}


# regions of other providers (RunPod's region group), registered when the provider is created
_OTHER_REGIONS: dict[str, RegionMeta] = {}


def register_regions(regions: dict[str, RegionMeta]) -> None:
    _OTHER_REGIONS.update(regions)


def region_meta(slug: str) -> RegionMeta:
    return DO_REGIONS.get(slug) or _OTHER_REGIONS.get(slug) or RegionMeta(slug, slug.upper(), 0.0, 0.0, None)
